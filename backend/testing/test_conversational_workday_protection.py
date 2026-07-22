"""Guard: conversational Workday protection manifest.

Validates that the manifest is well-formed and that every file listed under
PROTECT actually exists in the tree today. Deliberately makes NO assertions
about files marked SAFE-TO-CUT or RESTORE-ON-RESTORE (those are absent by
design while the restoration lane is mid-flight).

Safe to run standalone (pytest --noconftest --no-postgres): no DB, no fixtures,
no conftest imports. Added to the standalone smoke set via the normal
--standalone marker conventions used elsewhere in this directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MANIFEST_PATH = Path(__file__).resolve().parent / "conversational_workday_manifest.json"


def _resolve_repo_root() -> Path:
    candidates = (Path(__file__).resolve().parents[2], Path("/repo"))
    for candidate in candidates:
        if (candidate / "backend" / "testing" / MANIFEST_PATH.name).is_file():
            return candidate
    return candidates[0]


REPO_ROOT = _resolve_repo_root()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_manifest() -> dict:
    with MANIFEST_PATH.open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_manifest_exists():
    """The manifest file must be committed alongside this guard."""
    assert MANIFEST_PATH.exists(), (
        f"Protection manifest not found at {MANIFEST_PATH}. "
        "The file must be committed in the same directory as this test."
    )


def test_manifest_is_valid_json_with_required_keys():
    """Manifest must be valid JSON with required top-level keys."""
    manifest = _load_manifest()

    assert "_schema" in manifest, "manifest missing '_schema' key"
    assert manifest["_schema"] == "conversational-workday-protection-manifest-v1", (
        f"Unexpected schema version: {manifest['_schema']!r}"
    )

    assert "protect" in manifest, "manifest missing 'protect' key"
    assert isinstance(manifest["protect"], list), "'protect' must be a list"
    assert len(manifest["protect"]) > 0, "'protect' list must not be empty"

    assert "deleted_frontend" in manifest, "manifest missing 'deleted_frontend' key"
    assert isinstance(manifest["deleted_frontend"], list), "'deleted_frontend' must be a list"

    for entry in manifest["protect"]:
        assert "path" in entry, f"protect entry missing 'path': {entry}"
        assert "reason" in entry, f"protect entry missing 'reason': {entry!r}"
        assert "test_ids" in entry, f"protect entry missing 'test_ids': {entry!r}"
        assert isinstance(entry["test_ids"], list), f"'test_ids' must be a list in {entry['path']}"
        assert len(entry["test_ids"]) > 0, f"'test_ids' must not be empty in {entry['path']}"

    for entry in manifest["deleted_frontend"]:
        assert "path" in entry, f"deleted_frontend entry missing 'path': {entry}"
        assert "status" in entry, f"deleted_frontend entry missing 'status': {entry['path']}"
        assert entry["status"] in {"RESTORE-ON-RESTORE", "SAFE-TO-CUT"}, (
            f"Unknown status {entry['status']!r} in deleted_frontend entry {entry['path']}"
        )


def test_all_protect_paths_exist():
    """Every file listed under 'protect' must exist in the working tree today.

    If a protect-listed file is deleted, this test fails — surfacing the
    removal before any CI run can proceed without coverage for that seam.
    """
    manifest = _load_manifest()
    missing = []
    for entry in manifest["protect"]:
        full_path = REPO_ROOT / entry["path"]
        if not full_path.exists():
            missing.append(entry["path"])

    assert not missing, (
        "The following PROTECT-listed files are missing from the working tree. "
        "Either restore them or update the manifest (with operator sign-off):\n"
        + "\n".join(f"  - {p}" for p in missing)
    )


def test_no_protect_path_is_also_safe_to_cut():
    """No path may appear in both 'protect' and 'deleted_frontend' with SAFE-TO-CUT.

    This guards against a category error where the same test file is flagged
    as both live-coverage and dead/removable.
    """
    manifest = _load_manifest()
    protect_paths = {entry["path"] for entry in manifest["protect"]}
    safe_to_cut_paths = {
        entry["path"]
        for entry in manifest["deleted_frontend"]
        if entry.get("status") == "SAFE-TO-CUT"
    }
    overlap = protect_paths & safe_to_cut_paths
    assert not overlap, (
        "These paths appear in both 'protect' and 'deleted_frontend[status=SAFE-TO-CUT]' — "
        "a path cannot be both live coverage and dead:\n"
        + "\n".join(f"  - {p}" for p in sorted(overlap))
    )


def test_all_protect_test_ids_exist_in_their_files():
    """Every test_id listed in a protect entry must exist as a real function.

    A test can be deleted or renamed with the path guard still green — this
    check closes that gap by scanning the file for a matching def/async-def
    line. If a protected test is removed, this fails before CI can proceed
    without coverage for that seam.
    """
    import re

    manifest = _load_manifest()
    missing = []
    for entry in manifest["protect"]:
        full_path = REPO_ROOT / entry["path"]
        if not full_path.exists():
            # test_all_protect_paths_exist already covers this; skip here
            continue
        # Only validate test_ids for Python files — TypeScript/TSX test suites
        # use describe/it/test() patterns, not def, so the ids in those entries
        # are descriptive labels rather than function names.
        if not entry["path"].endswith(".py"):
            continue
        source = full_path.read_text(encoding="utf-8")
        for test_id in entry["test_ids"]:
            pattern = re.compile(
                r"^\s*(?:async\s+)?def\s+" + re.escape(test_id) + r"\s*\(",
                re.MULTILINE,
            )
            if not pattern.search(source):
                missing.append(f"{entry['path']}::{test_id}")

    assert not missing, (
        "The following PROTECT-listed test_ids no longer exist as function "
        "definitions in their files. Either restore the test or update the "
        "manifest (with operator sign-off):\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


def test_removed_backend_resident_seams_are_absent():
    """Deleted shadow-turn ORM classes must not reappear in models.py.

    Migration 0657 dropped the eight strategic-resident tables. If a bad
    restore reintroduces any of these as a SQLModel class definition, this
    test fails immediately — before any migration chain is run and before
    any CI job proceeds.
    """
    import re

    # Class names corresponding to the tables dropped in migration 0657.
    # SQLModel class names == PascalCase of the table name.
    DELETED_RESIDENT_CLASSES = (
        "StrategicResident",
        "StrategicResidentCheckpoint",
        "StrategicOperationReceipt",
        "StrategicWorkerMemoryProposal",
        "StrategicOperationApproval",
        "StrategicResidentSweepState",
        "StrategicResidentTaskTerminalEvent",
        "StrategicResidentOrdinaryTaskTerminalEvent",
    )

    models_path = REPO_ROOT / "backend" / "app" / "models.py"
    assert models_path.exists(), f"models.py not found at {models_path}"
    source = models_path.read_text(encoding="utf-8")

    reintroduced = []
    for class_name in DELETED_RESIDENT_CLASSES:
        # Match "class StrategicResident(" or "class StrategicResident:" but
        # not substrings of longer names (e.g. StrategicResidentCheckpoint
        # must not match a StrategicResidentCheckpointSomething guard).
        pattern = re.compile(
            r"^\s*class\s+" + re.escape(class_name) + r"\s*[\(:]",
            re.MULTILINE,
        )
        if pattern.search(source):
            reintroduced.append(class_name)

    assert not reintroduced, (
        "The following shadow-turn ORM classes were reintroduced into "
        "backend/app/models.py. These belong to the autonomous resident "
        "control plane that was permanently removed in migration 0657. "
        "Remove the class definitions or revert the restore:\n"
        + "\n".join(f"  - {c}" for c in reintroduced)
    )


def test_deleted_frontend_paths_are_absent():
    """Files deleted as part of the resident removal must NOT exist in the tree.

    If a deleted-frontend path reappears (e.g. during restoration), this test
    fails. That is intentional: the restoring author should then promote it
    from 'deleted_frontend' to 'protect' in the manifest.
    """
    manifest = _load_manifest()
    reappeared = []
    for entry in manifest["deleted_frontend"]:
        full_path = REPO_ROOT / entry["path"]
        if full_path.exists():
            reappeared.append((entry["path"], entry["status"]))

    if reappeared:
        lines = "\n".join(
            f"  - {p} (was: {s})" for p, s in reappeared
        )
        pytest.fail(
            "The following deleted-frontend paths have reappeared in the working tree. "
            "If these files were intentionally restored, move them to the 'protect' "
            "section of conversational_workday_manifest.json:\n" + lines
        )
