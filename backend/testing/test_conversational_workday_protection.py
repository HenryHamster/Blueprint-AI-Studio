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
import unittest
from pathlib import Path
from typing import cast

import pytest

from testing.fixgate_attribution import format_offender_marker
from testing.identity_extraction import (
    count_python_test_ids,
    extract_python_test_ids,
    extract_typescript_test_ids,
)

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
    with MANIFEST_PATH.open(encoding="utf-8") as fh:
        return cast(dict, json.load(fh))


def _extract_test_ids(path: str, source: str) -> set[str]:
    """Extract all test identities supported by a protected test file."""
    if path.endswith(".py"):
        return extract_python_test_ids(source)
    if path.endswith((".ts", ".tsx")):
        return extract_typescript_test_ids(source)
    return set()


def _find_completeness_violations(manifest: dict, repo_root: Path) -> list[dict]:
    """Return missing/extra IDs for entries explicitly marked complete."""
    violations = []
    for entry in manifest["protect"]:
        if entry.get("completeness") != "all":
            continue

        target_path = entry["path"]
        full_path = repo_root / target_path
        if not full_path.exists():
            continue

        actual_ids = _extract_test_ids(
            target_path,
            full_path.read_text(encoding="utf-8"),
        )
        listed_ids = set(entry["test_ids"])
        missing_from_manifest = sorted(actual_ids - listed_ids)
        extra_in_manifest = sorted(listed_ids - actual_ids)
        if missing_from_manifest or extra_in_manifest:
            violations.append(
                {
                    "path": target_path,
                    "missing": missing_from_manifest,
                    "extra": extra_in_manifest,
                }
            )
    return violations


def _find_existence_violations(manifest: dict, repo_root: Path) -> list[str]:
    """Return manifest identities no longer present in protected test files."""
    missing: list[str] = []
    for entry in manifest["protect"]:
        full_path = repo_root / entry["path"]
        if not full_path.exists():
            continue
        actual_ids = _extract_test_ids(
            entry["path"], full_path.read_text(encoding="utf-8")
        )
        missing.extend(
            f"{entry['path']}::{test_id}"
            for test_id in entry["test_ids"]
            if test_id not in actual_ids
        )
    return missing


def _protected_test_count(path: str, source: str) -> int | None:
    """Return the collector-faithful test count for a protected source file."""
    if path.endswith(".py"):
        return count_python_test_ids(source)
    if path.endswith((".ts", ".tsx")):
        return len(extract_typescript_test_ids(source))
    return None


def _find_test_count_mismatches(manifest: dict, repo_root: Path) -> list[str]:
    """Return protected entries whose declared count is not an exact snapshot."""
    mismatches: list[str] = []
    for entry in manifest["protect"]:
        path = entry["path"]
        full_path = repo_root / path
        if not full_path.exists():
            continue
        actual = _protected_test_count(path, full_path.read_text(encoding="utf-8"))
        if actual is None:
            mismatches.append(f"{path}: unsupported test file type")
            continue
        declared_count = entry["test_count"]
        if actual != declared_count:
            mismatches.append(
                f"{path}: actual={actual}, declared_exact={declared_count}"
            )
    return mismatches


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
        assert "test_count" in entry, f"protect entry missing 'test_count': {entry!r}"
        assert isinstance(entry["test_ids"], list), f"'test_ids' must be a list in {entry['path']}"
        assert len(entry["test_ids"]) > 0, f"'test_ids' must not be empty in {entry['path']}"
        if "completeness" in entry:
            assert entry["completeness"] == "all", (
                f"Unknown completeness marker in {entry['path']}: "
                f"{entry['completeness']!r}; expected 'all'"
            )
        assert isinstance(entry["test_count"], int) and not isinstance(entry["test_count"], bool), (
            f"'test_count' must be an integer in {entry['path']}"
        )
        assert entry["test_count"] > 0, f"'test_count' must be positive in {entry['path']}"

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
        + "\n"
        + format_offender_marker(missing)
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
        + "\n"
        + format_offender_marker(overlap)
    )


def test_no_duplicate_test_ids_in_protect_entry():
    """No protect entry may list the same test_id more than once.

    Duplicate test_ids create an illusion of coverage breadth without adding
    any actual guard — a test deleted once still appears to be listed in the
    manifest. Duplicates also mask count-mismatch errors in the per-file
    completeness checks (test_workday_invariant_suite_is_protected_with_all_tests
    and siblings) that compare len(actual) vs len(listed).
    """
    manifest = _load_manifest()
    duplicates = []
    for entry in manifest["protect"]:
        seen: set[str] = set()
        dupes: list[str] = []
        for tid in entry["test_ids"]:
            if tid in seen:
                dupes.append(tid)
            seen.add(tid)
        if dupes:
            duplicates.append(
                f"{entry['path']}: {dupes}"
            )
    assert not duplicates, (
        "The following protect entries contain duplicate test_ids. "
        "Duplicates weaken the manifest completeness guarantee — remove them:\n"
        + "\n".join(f"  - {d}" for d in duplicates)
        + "\n"
        + format_offender_marker(duplicates)
    )


def test_all_protect_test_ids_exist_in_their_files():
    """Every test_id listed in a protect entry must exist in its test file.

    Python test IDs use the canonical ``Class::method`` identity, while
    TypeScript/TSX test IDs are their it()/test()/describe() label strings.
    A test can be deleted or renamed with the path guard still green — this
    check closes that gap.
    """
    manifest = _load_manifest()
    missing = _find_existence_violations(manifest, REPO_ROOT)

    assert not missing, (
        "The following PROTECT-listed test_ids no longer exist in their files. "
        "Either restore the test or update the manifest (with operator sign-off):\n"
        + "\n".join(f"  - {m}" for m in missing)
        + "\n"
        + format_offender_marker(missing)
    )


def test_existence_and_completeness_use_the_canonical_class_qualified_identities(
    tmp_path: Path,
):
    """Same-named class methods stay distinct in both manifest protections."""
    source = (
        "class FirstTest:\n"
        "    def test_shared(self): pass\n"
        "class SecondTest:\n"
        "    def test_shared(self): pass\n"
    )
    path = "testing/test_identity_sample.py"
    target = tmp_path / path
    target.parent.mkdir()
    target.write_text(source, encoding="utf-8")
    manifest = {
        "protect": [
            {
                "path": path,
                "test_ids": ["FirstTest::test_shared", "SecondTest::test_shared"],
                "completeness": "all",
            }
        ]
    }

    assert _find_existence_violations(manifest, tmp_path) == []
    assert _find_completeness_violations(manifest, tmp_path) == []

    bare_manifest = {
        "protect": [
            {**manifest["protect"][0], "test_ids": ["test_shared"]}
        ]
    }
    assert _find_existence_violations(bare_manifest, tmp_path) == [
        f"{path}::test_shared"
    ]
    assert _find_completeness_violations(bare_manifest, tmp_path) == [
        {
            "path": path,
            "missing": ["FirstTest::test_shared", "SecondTest::test_shared"],
            "extra": ["test_shared"],
        }
    ]


def test_every_protected_file_meets_declared_test_count():
    """Every protected file must match its manifest test count exactly.

    The manifest's hand-picked ``test_ids`` remain a forward existence contract;
    this data-driven exact snapshot closes both directions of the reverse gap,
    so adding or deleting an undeclared test fails without hardcoding paths.
    """
    manifest = _load_manifest()
    mismatches = _find_test_count_mismatches(manifest, REPO_ROOT)

    assert not mismatches, (
        "Protected test files must match their declared test-count snapshot; "
        "update conversational_workday_manifest.json after an intentional inventory change:\n"
        + "\n".join(f"  - {item}" for item in mismatches)
        + "\n" + format_offender_marker(str(offender) for offender in mismatches)
    )


def test_new_floor_slack_relative_to_integration_branch_fails():
    """An integration-branch count lag is reported as a protected mismatch."""
    manifest = _load_manifest()
    entry = next(
        item for item in manifest["protect"]
        if item["path"] == "backend/testing/test_reaper.py"
    )
    actual = _protected_test_count(
        entry["path"],
        (REPO_ROOT / entry["path"]).read_text(encoding="utf-8"),
    )
    assert actual is not None and actual > 1

    integration_branch_manifest = json.loads(json.dumps(manifest))
    integration_entry = next(
        item for item in integration_branch_manifest["protect"]
        if item["path"] == entry["path"]
    )
    integration_entry["test_count"] = actual - 1

    assert _find_test_count_mismatches(
        integration_branch_manifest, REPO_ROOT
    ) == [f"{entry['path']}: actual={actual}, declared_exact={actual - 1}"]


def test_python_identity_extraction_includes_unittest_subclass_methods():
    """The count floor includes unittest classes outside pytest's name pattern."""
    source = (
        "import unittest\n"
        "class RunnerCase(unittest.TestCase):\n"
        "    pass\n"
        "class RunnerRegressionTests(RunnerCase):\n"
        "    def test_protected(self): pass\n"
        "class Helper:\n"
        "    def test_not_collected(self): pass\n"
    )

    assert extract_python_test_ids(source) == {"RunnerRegressionTests::test_protected"}
    assert count_python_test_ids(source) == 1


def test_python_identity_extraction_expands_inherited_unittest_methods():
    """A subclass carries its inherited ``test_*`` methods under its own name.

    ``unittest.TestLoader().getTestCaseNames`` reports ``test_inherited`` on
    ``Child``, so a static universe that only reads ``Child``'s own body
    undercounts the protected floor and silently drops duration identities.
    The expectation is derived from the live loader rather than hardcoded, so
    this cannot be satisfied by teaching the extractor a literal answer.
    """
    source = (
        "import unittest\n"
        "class Base(unittest.TestCase):\n"
        "    def test_inherited(self): pass\n"
        "class Child(Base):\n"
        "    pass\n"
        "class TestOverrider(Base):\n"
        "    def test_inherited(self): pass\n"
        "    def test_own(self): pass\n"
    )
    namespace: dict[str, object] = {}
    exec(compile(source, "<identity-extraction-reproducer>", "exec"), namespace)
    loader = unittest.TestLoader()
    collector_ids = {
        f"{name}::{method}"
        for name in ("Base", "Child", "TestOverrider")
        for method in loader.getTestCaseNames(namespace[name])
    }

    assert extract_python_test_ids(source) == collector_ids
    assert "Child::test_inherited" in collector_ids


def test_removed_backend_resident_seams_are_absent():
    """Deleted shadow-turn ORM classes must not reappear in any backend module.

    Migration 0657 dropped the eight strategic-resident tables. If a bad
    restore reintroduces any of these as a class definition in any backend/app
    Python module (not only models.py), this test fails immediately — before
    any migration chain is run and before any CI job proceeds.
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

    backend_app_dir = REPO_ROOT / "backend" / "app"
    assert backend_app_dir.is_dir(), f"backend/app/ not found at {backend_app_dir}"
    py_files = list(backend_app_dir.rglob("*.py"))
    assert py_files, f"No .py files found under {backend_app_dir}"

    reintroduced = []
    for class_name in DELETED_RESIDENT_CLASSES:
        # Match "class StrategicResident(" or "class StrategicResident:" but
        # not substrings of longer names (e.g. StrategicResidentCheckpoint
        # must not match a StrategicResidentCheckpointSomething guard).
        pattern = re.compile(
            r"^\s*class\s+" + re.escape(class_name) + r"\s*[\(:]",
            re.MULTILINE,
        )
        for py_file in py_files:
            source = py_file.read_text(encoding="utf-8")
            if pattern.search(source):
                rel = py_file.relative_to(REPO_ROOT)
                reintroduced.append(f"{class_name} in {rel}")

    assert not reintroduced, (
        "The following shadow-turn ORM classes were reintroduced into backend "
        "source files. These belong to the autonomous resident control plane "
        "that was permanently removed in migration 0657. Remove the class "
        "definitions or revert the restore:\n"
        + "\n".join(f"  - {c}" for c in reintroduced) + "\n" + format_offender_marker(str(offender) for offender in reintroduced)
    )


def test_agent_dispatch_gate_is_guarded():
    """The agent-side dispatch gate symbols must remain defined in handlers/index.ts.

    isResidentSessionTask guards the entry point that routes a marked
    strategic_review task into the conversational Workday session handler.
    dispatchStrategic is the internal handler that calls isResidentSessionTask
    and branches to handleStrategicResident vs. handleStrategic. If either
    symbol is removed, the resident session path silently falls back to
    one-shot strategic for all Workday conversations.
    """
    import re

    handler_index = REPO_ROOT / "agent" / "src" / "handlers" / "index.ts"
    assert handler_index.exists(), (
        f"agent/src/handlers/index.ts not found at {handler_index}. "
        "This file is the dispatch gate for the conversational Workday session."
    )
    source = handler_index.read_text(encoding="utf-8")

    # isResidentSessionTask must be exported as a named function
    assert re.search(
        r"export\s+function\s+isResidentSessionTask\s*\(",
        source,
        re.MULTILINE,
    ), (
        "isResidentSessionTask is no longer exported from agent/src/handlers/index.ts. "
        "This function guards the entry point for the conversational Workday session — "
        "restore it or update this guard with operator sign-off."
    )

    # dispatchStrategic must be defined as the internal handler variable
    assert re.search(
        r"(?:const|function)\s+dispatchStrategic\s*[=:(]",
        source,
        re.MULTILINE,
    ), (
        "dispatchStrategic is no longer defined in agent/src/handlers/index.ts. "
        "This handler calls isResidentSessionTask to route Workday conversations — "
        "restore it or update this guard with operator sign-off."
    )

    # handleStrategicResident must be imported so the resident path can be reached
    assert "handleStrategicResident" in source, (
        "handleStrategicResident is no longer referenced in agent/src/handlers/index.ts. "
        "The conversational Workday dispatch gate requires this import to route "
        "marked strategic_review tasks to the persistent session handler."
    )


def test_agent_session_provider_is_guarded():
    """The session provider symbols must remain defined in providers/resident-session.ts.

    startResidentClaudeSession is the factory that spawns and holds the long-lived
    warm Claude process across many operator turns. ResidentSession is the interface
    that the handler uses to send turns, handle interrupts, and check liveness. If
    either is removed, the persistent conversational session degrades silently to
    one-shot behaviour (every turn spawns a cold process, losing context).
    """
    import re

    provider_path = REPO_ROOT / "agent" / "src" / "providers" / "resident-session.ts"
    assert provider_path.exists(), (
        f"agent/src/providers/resident-session.ts not found at {provider_path}. "
        "This file is the session provider for the conversational Workday session."
    )
    source = provider_path.read_text(encoding="utf-8")

    # startResidentClaudeSession must be exported as the session factory
    assert re.search(
        r"export\s+(?:async\s+)?function\s+startResidentClaudeSession\s*\(",
        source,
        re.MULTILINE,
    ), (
        "startResidentClaudeSession is no longer exported from "
        "agent/src/providers/resident-session.ts. This factory creates the long-lived "
        "warm Claude process that holds conversation context across turns — "
        "restore it or update this guard with operator sign-off."
    )

    # ResidentSession interface must be exported (the handler depends on it)
    assert re.search(
        r"export\s+interface\s+ResidentSession\s*\{",
        source,
        re.MULTILINE,
    ), (
        "ResidentSession interface is no longer exported from "
        "agent/src/providers/resident-session.ts. This interface is the contract "
        "between the session provider and the handler (sendTurn, interrupt, close, "
        "isAlive) — restore it or update this guard with operator sign-off."
    )


def test_agent_stream_provider_is_guarded():
    """The stream provider symbols must remain defined in providers/resident-stream.ts.

    isStrategicMutation and isMessageSend classify NDJSON tool-use lines during a
    resident turn — they set the lease-renewing appliedStrategicOp flag and the
    durable-reply sentOperatorMessage flag respectively. parseResidentLine is the
    primary parser that converts raw stdout into typed ResidentStreamEvents. If any
    of these is removed, the session either fails to renew its lease on strategic
    mutations or loses the reply-detection that triggers durable ProjectMessage
    creation.
    """
    import re

    provider_path = REPO_ROOT / "agent" / "src" / "providers" / "resident-stream.ts"
    assert provider_path.exists(), (
        f"agent/src/providers/resident-stream.ts not found at {provider_path}. "
        "This file is the stream parser/classifier for the conversational Workday session."
    )
    source = provider_path.read_text(encoding="utf-8")

    # isStrategicMutation must be exported — gates the lease-renewing op flag
    assert re.search(
        r"export\s+function\s+isStrategicMutation\s*\(",
        source,
        re.MULTILINE,
    ), (
        "isStrategicMutation is no longer exported from "
        "agent/src/providers/resident-stream.ts. This classifier sets the "
        "appliedStrategicOp flag that renews the session lease on strategic mutations — "
        "restore it or update this guard with operator sign-off."
    )

    # isMessageSend must be exported — gates durable ProjectMessage creation
    assert re.search(
        r"export\s+function\s+isMessageSend\s*\(",
        source,
        re.MULTILINE,
    ), (
        "isMessageSend is no longer exported from "
        "agent/src/providers/resident-stream.ts. This classifier gates the "
        "sentOperatorMessage flag that triggers durable ProjectMessage creation — "
        "restore it or update this guard with operator sign-off."
    )

    # parseResidentLine must be exported — primary stdout-to-event parser
    assert re.search(
        r"export\s+function\s+parseResidentLine\s*\(",
        source,
        re.MULTILINE,
    ), (
        "parseResidentLine is no longer exported from "
        "agent/src/providers/resident-stream.ts. This function is the primary "
        "parser that converts raw Claude stdout lines into typed ResidentStreamEvents — "
        "restore it or update this guard with operator sign-off."
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


# ---------------------------------------------------------------------------
# R1 / R3 guards — removed autonomous control-plane seams
# ---------------------------------------------------------------------------


def test_removed_authority_modules_are_absent():
    """R1: Deleted authority execution/validation modules must not reappear.

    Migration 0657 permanently removed the autonomous resident control plane,
    including the strategic-operation authority execution and validation modules.
    If either file reappears as a module under backend/app/, a bad restore is
    in progress. Additionally, strategic_authority_locks.py must stay a stub
    without code references to the removed StrategicResident class (the
    docstring's historical mention is tolerated).
    """
    import re

    backend_app_dir = REPO_ROOT / "backend" / "app"
    assert backend_app_dir.is_dir(), f"backend/app/ not found at {backend_app_dir}"

    # The two deleted authority modules must not exist as files on disk.
    DELETED_AUTHORITY_MODULES = (
        "strategic_operation_authority_execution.py",
        "strategic_operation_authority_validation.py",
    )
    reappeared = []
    for module_name in DELETED_AUTHORITY_MODULES:
        path = backend_app_dir / module_name
        if path.exists():
            reappeared.append(f"backend/app/{module_name}")

    assert not reappeared, (
        "The following authority modules from the removed autonomous resident "
        "control plane have reappeared on disk. These were permanently removed "
        "in migration 0657 — remove them:\n"
        + "\n".join(f"  - {m}" for m in reappeared) + "\n" + format_offender_marker(str(offender) for offender in reappeared)
    )

    # strategic_authority_locks.py must exist and must not contain code
    # references to StrategicResident (the docstring's historical mentions
    # are allowed context; actual imports or instantiation would signal a
    # partial restore of the authority plane).
    locks_path = backend_app_dir / "strategic_authority_locks.py"
    assert locks_path.exists(), (
        "strategic_authority_locks.py no longer exists — it should remain as "
        "a stub for legacy import compatibility."
    )
    source = locks_path.read_text(encoding="utf-8")

    assert not re.search(r"StrategicResident\(", source), (
        "strategic_authority_locks.py contains a StrategicResident instantiation "
        "call — this class was removed in migration 0657. Keep the file as a "
        "docstring-only stub."
    )
    assert not re.search(r"(?:from|import)\s+.*StrategicResident", source), (
        "strategic_authority_locks.py imports StrategicResident — "
        "this class was removed in migration 0657. Keep the file as a "
        "docstring-only stub."
    )


def test_authority_plane_field_names_are_absent():
    """R2/R3: engine_owner and authority_generation must not reappear as ORM columns.

    The boundary map (knowledge 2df23870) explicitly names re-adding
    engine_owner or authority_generation as a resurrection of the removed
    second-engine authority row. Guard that neither field name appears as a
    SQLModel/SQLAlchemy column definition (Field(...) or Column(...) assignment)
    in any backend/app Python module.
    """
    import re

    backend_app_dir = REPO_ROOT / "backend" / "app"
    assert backend_app_dir.is_dir(), f"backend/app/ not found at {backend_app_dir}"
    py_files = list(backend_app_dir.rglob("*.py"))
    assert py_files, f"No .py files found under {backend_app_dir}"

    # Match column-definition patterns: `engine_owner: ... = Field(` or
    # `engine_owner = Column(` — i.e. the identifier used as the column name
    # in a class body, not a reference in a docstring or comment.
    FIELD_NAMES = ("engine_owner", "authority_generation")
    COLUMN_PATTERN = re.compile(
        r"^\s*(?:"
        + "|".join(re.escape(f) for f in FIELD_NAMES)
        + r")\s*(?::[^=\n]*)?\s*=\s*(?:Field|Column)\s*\(",
        re.MULTILINE,
    )

    reintroduced = []
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        m = COLUMN_PATTERN.search(source)
        if m:
            rel = py_file.relative_to(REPO_ROOT)
            reintroduced.append(f"{rel} (match: {m.group().strip()!r})")

    assert not reintroduced, (
        "The following files reintroduce engine_owner or authority_generation as "
        "ORM column definitions. These field names belong to the removed second-engine "
        "authority row (migration 0657). Remove them or update this guard with "
        "operator sign-off:\n"
        + "\n".join(f"  - {r}" for r in reintroduced) + "\n" + format_offender_marker(str(offender) for offender in reintroduced)
    )


def test_no_removed_resident_strategic_backend_modules():
    """R3: No strategic_resident_*.py modules beyond the kept session file.

    Migration 0657 deleted every strategic_resident_*.py backend module (~70
    files). Only strategic_resident_session.py was restored as part of the
    conversational Workday path (PR #16075). Any other strategic_resident_*.py
    file reappearing under backend/app/ signals a partial restore of the
    autonomous resident subsystem.
    """
    backend_app_dir = REPO_ROOT / "backend" / "app"
    assert backend_app_dir.is_dir(), f"backend/app/ not found at {backend_app_dir}"

    # The only strategic_resident_*.py module intentionally kept for the
    # conversational Workday session path. All others stay permanently deleted.
    KEPT_RESIDENT_MODULES = {"strategic_resident_session.py"}

    resident_files = sorted(backend_app_dir.glob("strategic_resident_*.py"))
    unexpected = [f.name for f in resident_files if f.name not in KEPT_RESIDENT_MODULES]

    assert not unexpected, (
        "Unexpected strategic_resident_*.py module(s) found under backend/app/. "
        "Only strategic_resident_session.py is kept for the conversational "
        "Workday path. These belong to the deleted autonomous resident "
        "subsystem (migration 0657) and must be removed:\n"
        + "\n".join(f"  - {m}" for m in unexpected) + "\n" + format_offender_marker(str(offender) for offender in unexpected)
    )


def test_protect_entries_marked_complete_have_all_tests():
    """Every manifest entry marked completeness=all covers its whole file.

    Entries without the marker intentionally retain partial protection semantics.
    The marker is explicit so adding a new test to a complete suite cannot make
    the manifest silently stale.
    """
    manifest = _load_manifest()
    violations = _find_completeness_violations(manifest, REPO_ROOT)

    assert not violations, (
        "The following complete protect entries do not exactly match their "
        "test files. Add newly-created tests, remove stale IDs, or remove the "
        "completeness marker only when partial protection is intentional:\n"
        + "\n".join(
            f"  - {violation['path']}: "
            f"missing={violation['missing']!r}, extra={violation['extra']!r}"
            for violation in violations
        )
        + "\n" + format_offender_marker(str(violation['path']) for violation in violations)
    )


def test_completeness_markers_identify_the_existing_full_suites():
    """Only suites with former reverse guards opt into full completeness."""
    manifest = _load_manifest()
    marked = {
        entry["path"]
        for entry in manifest["protect"]
        if entry.get("completeness") == "all"
    }
    assert len(marked) == 11
    assert all(
        entry.get("completeness") in {None, "all"}
        for entry in manifest["protect"]
    )
    assert "backend/testing/test_reaper.py" not in marked


def test_extract_test_ids_dispatches_python_and_typescript():
    """The generic extractor covers Python identities, it.each, and test labels."""
    assert _extract_test_ids(
        "example.py",
        "def test_python_case():\n    pass\n",
    ) == {"test_python_case"}
    assert _extract_test_ids(
        "example.test.tsx",
        "it('it case', () => {})\n"
        "it.each([['x']])('each %s', () => {})\n"
        'test("test case", () => {})\n'
        "test.each([[1]])(`test each $value`, () => {})\n",
    ) == {"it case", "each %s", "test case", "test each $value"}


def test_extractor_uses_describe_paths_and_expands_parameter_rows():
    """Canonical IDs retain suite ancestry, escapes, duplicates, and each rows."""
    source = r'''
describe("outer suite", () => {
  describe(
    'inner \'suite\'',
    () => {
      it("same label", () => {});
      it("same label", () => {});
      it.each([
        ["first", { nested: true }],
        ["second", { nested: false }],
      ])(
        'row %s',
        (name) => {}
      );
    },
  );
});
'''

    assert extract_typescript_test_ids(source) == {
        "outer suite > inner 'suite' > same label",
        "outer suite > inner 'suite' > same label [duplicate 2]",
        "outer suite > inner 'suite' > row %s [case 1]",
        "outer suite > inner 'suite' > row %s [case 2]",
    }


def test_completeness_checker_ignores_partial_and_reports_missing_and_extra(tmp_path):
    """Partial entries stay partial while complete entries fail both directions."""
    complete = tmp_path / "complete.py"
    complete.write_text(
        "def test_present():\n    pass\n\n"
        "def test_new():\n    pass\n",
        encoding="utf-8",
    )
    partial = tmp_path / "partial.py"
    partial.write_text("def test_unlisted():\n    pass\n", encoding="utf-8")
    manifest = {
        "protect": [
            {
                "path": "complete.py",
                "completeness": "all",
                "test_ids": ["test_present", "test_stale"],
            },
            {"path": "partial.py", "test_ids": ["test_other"]},
        ]
    }

    assert _find_completeness_violations(manifest, tmp_path) == [
        {
            "path": "complete.py",
            "missing": ["test_new"],
            "extra": ["test_stale"],
        }
    ]


def test_no_removed_resident_task_type_in_task_capabilities():
    """R3: The strategic_resident_turn TaskType must not reappear anywhere.

    Migration 0657 removed the strategic_resident_turn task type along with the
    autonomous resident subsystem. If this TaskType value is re-added to any
    backend/app module (dispatch, spawning, lifecycle, capabilities) or to
    agent/src handler files, a bad restore could resurrect resident-turn
    processing while file-based guards elsewhere remain green.

    Scans the full backend/app tree (all *.py files) and agent/src/handlers/
    (all *.ts files), matching the pattern used by the sibling ORM-class and
    authority-module guards.
    """
    backend_app_dir = REPO_ROOT / "backend" / "app"
    assert backend_app_dir.is_dir(), f"backend/app/ not found at {backend_app_dir}"

    py_files = list(backend_app_dir.rglob("*.py"))
    assert py_files, "No .py files found under backend/app/"

    hits = []
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        if "strategic_resident_turn" in source:
            hits.append(str(py_file.relative_to(REPO_ROOT)))

    agent_handlers_dir = REPO_ROOT / "agent" / "src" / "handlers"
    if agent_handlers_dir.is_dir():
        for ts_file in agent_handlers_dir.rglob("*.ts"):
            source = ts_file.read_text(encoding="utf-8")
            if "strategic_resident_turn" in source:
                hits.append(str(ts_file.relative_to(REPO_ROOT)))

    assert not hits, (
        "strategic_resident_turn was found in the following file(s):\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\nThis TaskType belonged to the autonomous resident control plane "
        "removed in migration 0657. Remove it to prevent reinstating the "
        "resident turn processing path." + "\n" + format_offender_marker(str(offender) for offender in hits)
    )


# ---------------------------------------------------------------------------
# Unit tests for the shared TypeScript identity extractor
#
# These tests verify that the manifest-completeness guards collect test labels
# regardless of which JS/TS quote style is used. A single-quote-only regex
# would silently miss it("…") and it(`…`) forms, allowing a newly-added test
# to bypass the anti-deletion guarantee without any guard failure.
# ---------------------------------------------------------------------------


def test_ts_label_extraction_single_quoted():
    """The shared extractor collects single-quoted it() labels."""
    source = "  it('finds single-quoted label', () => { expect(1).toBe(1) })\n"
    labels = extract_typescript_test_ids(source)
    assert labels == {"finds single-quoted label"}, (
        f"Expected {{'finds single-quoted label'}}, got {labels!r}"
    )


def test_ts_label_extraction_double_quoted():
    """The shared extractor collects double-quoted it() labels.

    A regex that only matches single quotes silently misses this form, so a
    test added as it("new test") would escape the manifest-completeness check.
    """
    source = '  it("finds double-quoted label", () => { expect(1).toBe(1) })\n'
    labels = extract_typescript_test_ids(source)
    assert labels == {"finds double-quoted label"}, (
        f"Expected {{'finds double-quoted label'}}, got {labels!r}"
    )


def test_ts_label_extraction_backtick_quoted():
    """The shared extractor collects backtick-quoted it() labels.

    A regex that only matches single quotes silently misses this form, so a
    test added as it(`new test`) would escape the manifest-completeness check.
    """
    source = "  it(`finds backtick-quoted label`, () => { expect(1).toBe(1) })\n"
    labels = extract_typescript_test_ids(source)
    assert labels == {"finds backtick-quoted label"}, (
        f"Expected {{'finds backtick-quoted label'}}, got {labels!r}"
    )


def test_ts_label_extraction_mixed_quote_styles():
    """The shared extractor collects all three quote styles in one file."""
    source = (
        "  it('single-quoted', () => {})\n"
        '  it("double-quoted", () => {})\n'
        "  it(`backtick-quoted`, () => {})\n"
    )
    labels = extract_typescript_test_ids(source)
    assert labels == {"single-quoted", "double-quoted", "backtick-quoted"}, (
        f"Expected all three quote styles captured, got {labels!r}"
    )


def test_ts_label_extraction_single_quote_only_regex_misses_double_quoted():
    """Demonstrate the gap: old single-quote regex silently misses double-quoted labels.

    This test documents the negative case: using a single-quote-only pattern
    (the previous implementation) on a source that contains only a
    double-quoted it() call produces an empty set — no guard failure would
    fire for a manifest that does not list the label.  The shared extractor
    fixes this by handling all three quote styles.
    """
    import re

    source = '  it("unlisted double-quoted test", () => { expect(1).toBe(1) })\n'

    # The old single-quote-only regex misses this label entirely.
    old_regex_result = set(re.findall(
        r"^\s*it\s*\(\s*'([^']+)'",
        source,
        re.MULTILINE,
    ))
    assert old_regex_result == set(), (
        "Sanity check failed: old single-quote regex should return nothing for "
        f"a double-quoted label, but got {old_regex_result!r}"
        + "\n"
        + format_offender_marker(old_regex_result)
    )

    # The fixed helper correctly finds the label.
    fixed_result = extract_typescript_test_ids(source)
    assert fixed_result == {"unlisted double-quoted test"}, (
        f"Fixed helper should find the double-quoted label, got {fixed_result!r}"
    )


def test_ts_label_extraction_single_quote_only_regex_misses_backtick():
    """Demonstrate the gap: old single-quote regex silently misses backtick labels."""
    import re

    source = "  it(`unlisted backtick test`, () => { expect(1).toBe(1) })\n"

    old_regex_result = set(re.findall(
        r"^\s*it\s*\(\s*'([^']+)'",
        source,
        re.MULTILINE,
    ))
    assert old_regex_result == set(), (
        "Sanity check failed: old single-quote regex should return nothing for "
        f"a backtick label, but got {old_regex_result!r}"
        + "\n"
        + format_offender_marker(old_regex_result)
    )

    fixed_result = extract_typescript_test_ids(source)
    assert fixed_result == {"unlisted backtick test"}, (
        f"Fixed helper should find the backtick label, got {fixed_result!r}"
    )


def test_reaper_resident_exemption_seam_is_guarded():
    """The resident reaper-exemption seam in reaper_orphan_classify.py must remain intact.

    _classify_resident_session exempts conversational Workday sessions from
    progress-staleness reaping — a warm-idle session between operator turns must
    not be reaped. This test verifies:
    (a) _classify_resident_session is still defined in reaper_orphan_classify.py
        (the production seam that enforces the carve-out),
    (b) both resident-exemption tests exist in test_reaper.py, and
    (c) both tests are listed in the protect manifest so deletion is caught.
    """
    import re

    # (a) Production seam must still exist.
    classify_path = REPO_ROOT / "backend" / "app" / "reaper_orphan_classify.py"
    assert classify_path.exists(), (
        "backend/app/reaper_orphan_classify.py not found — this file hosts the "
        "resident reaper-exemption logic (_classify_resident_session)."
    )
    classify_source = classify_path.read_text(encoding="utf-8")
    assert re.search(
        r"^\s*def\s+_classify_resident_session\s*\(",
        classify_source,
        re.MULTILINE,
    ), (
        "_classify_resident_session is no longer defined in reaper_orphan_classify.py. "
        "This function exempts conversational Workday sessions from progress-staleness "
        "reaping — restore it or update this guard with operator sign-off."
    )

    # (b) Both resident tests must exist in test_reaper.py.
    RESIDENT_REAPER_TESTS = (
        "test_classify_orphan_resident_session_exempt_from_progress_staleness",
        "test_classify_resident_session_warm_idle_not_reaped",
    )
    test_reaper_path = REPO_ROOT / "backend" / "testing" / "test_reaper.py"
    assert test_reaper_path.exists(), (
        "backend/testing/test_reaper.py not found — this file covers the reaper exemption seam."
    )
    reaper_source = test_reaper_path.read_text(encoding="utf-8")
    missing_tests = [
        t for t in RESIDENT_REAPER_TESTS
        if not re.search(r"^\s*def\s+" + re.escape(t) + r"\s*\(", reaper_source, re.MULTILINE)
    ]
    assert not missing_tests, (
        "The following resident reaper-exemption tests no longer exist in test_reaper.py. "
        "Restore them or update this guard with operator sign-off:\n"
        + "\n".join(f"  - {t}" for t in missing_tests) + "\n" + format_offender_marker(str(offender) for offender in missing_tests)
    )

    # (c) Both tests must be listed in the manifest protect entry for test_reaper.py.
    manifest = _load_manifest()
    reaper_entries = [e for e in manifest["protect"] if e["path"] == "backend/testing/test_reaper.py"]
    assert reaper_entries, (
        "backend/testing/test_reaper.py is not in the protect section of the manifest. "
        "Add it with the resident reaper-exemption test_ids."
    )
    listed = set(reaper_entries[0]["test_ids"])
    not_listed = [t for t in RESIDENT_REAPER_TESTS if t not in listed]
    assert not not_listed, (
        "The following resident reaper-exemption tests exist in test_reaper.py but "
        "are not listed in the protect manifest:\n"
        + "\n".join(f"  - {t}" for t in not_listed) + "\n" + format_offender_marker(str(offender) for offender in not_listed)
    )


def test_ts_it_each_label_extraction_double_quoted():
    """The shared extractor collects double-quoted it.each templates."""
    source = (
        "  it.each([\n"
        "    ['a'], ['b'],\n"
        "  ])(\"handles %s in double quotes\", (val) => { expect(val).toBeTruthy() })\n"
    )
    labels = extract_typescript_test_ids(source)
    assert labels == {
        "handles %s in double quotes [case 1]",
        "handles %s in double quotes [case 2]",
    }, (
        f"Expected double-quoted it.each template captured, got {labels!r}"
    )


def test_ts_it_each_label_extraction_backtick():
    """The shared extractor collects backtick it.each templates."""
    source = (
        "  it.each([\n"
        "    ['a'], ['b'],\n"
        "  ])(`handles %s in backticks`, (val) => { expect(val).toBeTruthy() })\n"
    )
    labels = extract_typescript_test_ids(source)
    assert labels == {
        "handles %s in backticks [case 1]",
        "handles %s in backticks [case 2]",
    }, (
        f"Expected backtick it.each template captured, got {labels!r}"
    )


def test_ts_label_extraction_ignores_non_leading_it_calls():
    """The shared extractor ignores inline 'it' mentions."""
    source = (
        "  it('valid test label', () => {\n"
        "    // a comment mentioning it('inline') or describe it('as follows')\n"
        "    expect(doIt('something')).toBe(true)\n"
        "  })\n"
    )
    labels = extract_typescript_test_ids(source)
    # Only the first line should be collected; inline mentions must be ignored.
    assert labels == {"valid test label"}, (
        f"Expected only the leading it() to be collected, got {labels!r}"
    )


def test_ts_label_extraction_empty_source():
    """The shared extractor returns an empty set for an empty source string."""
    assert extract_typescript_test_ids("") == set()
