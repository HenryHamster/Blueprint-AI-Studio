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
    """Every test_id listed in a protect entry must exist in its test file.

    For Python files: scans for a matching def/async-def line (exact function
    name). For TypeScript/TSX files: scans for the test_id as a quoted literal
    (the it()/test()/describe() label string). A test can be deleted or renamed
    with the path guard still green — this check closes that gap.
    """
    import re

    manifest = _load_manifest()
    missing = []
    for entry in manifest["protect"]:
        full_path = REPO_ROOT / entry["path"]
        if not full_path.exists():
            # test_all_protect_paths_exist already covers this; skip here
            continue
        source = full_path.read_text(encoding="utf-8")
        if entry["path"].endswith(".py"):
            for test_id in entry["test_ids"]:
                pattern = re.compile(
                    r"^\s*(?:async\s+)?def\s+" + re.escape(test_id) + r"\s*\(",
                    re.MULTILINE,
                )
                if not pattern.search(source):
                    missing.append(f"{entry['path']}::{test_id}")
        elif entry["path"].endswith((".ts", ".tsx")):
            # TypeScript test suites use describe()/it()/test() with string
            # labels. Validate each test_id by searching for it as a quoted
            # string literal anywhere in the file.
            for test_id in entry["test_ids"]:
                # Match the label inside single or double quotes
                pattern = re.compile(re.escape(test_id))
                if not pattern.search(source):
                    missing.append(f"{entry['path']}::{test_id}")

    assert not missing, (
        "The following PROTECT-listed test_ids no longer exist in their files. "
        "Either restore the test or update the manifest (with operator sign-off):\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


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
        + "\n".join(f"  - {c}" for c in reintroduced)
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
        + "\n".join(f"  - {m}" for m in reappeared)
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
        + "\n".join(f"  - {m}" for m in unexpected)
    )


def test_workday_invariant_suite_is_protected_with_all_tests():
    """The Workday invariant suite must appear in the protect manifest with all tests.

    test_workday_resident_invariants.py is the primary guard for conversational
    Workday seams. Deleting or emptying it without operator sign-off must trip a
    manifest guard. This test verifies: (a) the file is in the protect section,
    (b) every def/async-def test function in the actual file is listed, and
    (c) the count matches, so a silently-added test doesn't escape the manifest.
    """
    import re

    manifest = _load_manifest()
    invariant_path = "backend/testing/test_workday_resident_invariants.py"

    # (a) The file must appear in the protect section.
    entries = [e for e in manifest["protect"] if e["path"] == invariant_path]
    assert entries, (
        f"{invariant_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the invariant suite "
        "is caught at every CI run."
    )
    entry = entries[0]

    # (b/c) Every test function in the file must be listed and counts must match.
    full_path = REPO_ROOT / invariant_path
    if not full_path.exists():
        pytest.skip(f"{invariant_path} not found — test_all_protect_paths_exist covers the missing-file case")

    source = full_path.read_text(encoding="utf-8")
    actual_tests = re.findall(
        r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(",
        source,
        re.MULTILINE,
    )
    listed_tests = entry["test_ids"]

    missing_from_manifest = sorted(set(actual_tests) - set(listed_tests))
    extra_in_manifest = sorted(set(listed_tests) - set(actual_tests))

    assert not missing_from_manifest, (
        "These test functions exist in test_workday_resident_invariants.py but "
        "are NOT listed in the protect manifest. Add them so deletion of any "
        "invariant test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "test_workday_resident_invariants.py. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_strategic_resident_session_is_protected_with_all_tests():
    """The strategic resident session suite must appear in the manifest with all tests.

    backend/testing/test_strategic_resident_session.py is the primary backend
    seam for the operator-driven Workday session lifecycle (start, stop, lease,
    handoff). Deleting or emptying it without operator sign-off must trip this
    guard. Verifies: (a) the file is in the protect section, (b) every
    def/async-def test function in the file is listed, and (c) counts match so
    a silently-added test doesn't escape the manifest.
    """
    import re

    manifest = _load_manifest()
    target_path = "backend/testing/test_strategic_resident_session.py"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the resident session "
        "suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")
    actual_tests = re.findall(
        r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(",
        source,
        re.MULTILINE,
    )
    listed_tests = entry["test_ids"]

    missing_from_manifest = sorted(set(actual_tests) - set(listed_tests))
    extra_in_manifest = sorted(set(listed_tests) - set(actual_tests))

    assert not missing_from_manifest, (
        "These test functions exist in test_strategic_resident_session.py but "
        "are NOT listed in the protect manifest. Add them so deletion of any "
        "resident session test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "test_strategic_resident_session.py. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_strategic_resident_agent_handler_is_protected_with_all_tests():
    """The strategic resident agent handler test suite must appear in the manifest with all tests.

    agent/src/handlers/strategic-resident.test.ts is the deepest coverage of
    the resident session path on the agent side — it covers operator reply,
    compaction restart, workspace recovery, corrective tick, and provider-error
    guard. Deleting or emptying it without operator sign-off must trip this
    guard. Verifies: (a) the file is in the protect section, (b) every it()/
    it.each() test label in the file is listed, and (c) counts match.
    """
    import re

    manifest = _load_manifest()
    target_path = "agent/src/handlers/strategic-resident.test.ts"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the resident handler "
        "test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")

    # Collect all plain it('label', ...) test labels from lines starting with it(.
    actual_labels: set[str] = set(re.findall(
        r"^\s*it\s*\(\s*'([^']+)'",
        source,
        re.MULTILINE,
    ))
    # Collect it.each(...)('template', ...) template strings — the template may
    # appear on the same line or the next line after the closing ) of each(...).
    for m in re.finditer(r"it\.each\b[\s\S]*?\)\s*\(\s*\n?\s*'([^']+)'", source):
        actual_labels.add(m.group(1))

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in strategic-resident.test.ts but are NOT "
        "listed in the protect manifest. Add them so deletion of any resident "
        "handler test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "strategic-resident.test.ts. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_no_removed_resident_task_type_in_task_capabilities():
    """R3: The strategic_resident_turn TaskType must not reappear.

    Migration 0657 removed the strategic_resident_turn task type along with the
    autonomous resident subsystem. If this TaskType value is re-added to the
    TaskType enum in task_capabilities.py (or referenced anywhere in the
    module), a bad restore could resurrect resident-turn processing with all
    file-based guards still green.
    """
    task_cap_path = REPO_ROOT / "backend" / "app" / "task_capabilities.py"
    assert task_cap_path.exists(), (
        f"task_capabilities.py not found at {task_cap_path}"
    )
    source = task_cap_path.read_text(encoding="utf-8")

    assert "strategic_resident_turn" not in source, (
        "strategic_resident_turn was found in task_capabilities.py. "
        "This TaskType belonged to the autonomous resident control plane "
        "removed in migration 0657. Remove it to prevent reinstating the "
        "resident turn processing path."
    )
