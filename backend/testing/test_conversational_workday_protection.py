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
    with MANIFEST_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _extract_ts_it_labels(source: str) -> set[str]:
    """Extract all it('...') / it("...") / it(`...`) labels from TS source.

    Handles all three JS/TS quote styles.  A single-quote-only regex would
    silently miss labels written as it("…") or it(`…`), allowing a new test
    to escape the manifest-completeness check and nullify the anti-deletion
    guarantee.
    """
    import re

    labels: set[str] = set()
    for m in re.finditer(
        r"""^\s*it\s*\(\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)""",
        source,
        re.MULTILINE,
    ):
        label = m.group(1) or m.group(2) or m.group(3)
        if label:
            labels.add(label)
    return labels


def _extract_ts_it_each_labels(source: str) -> set[str]:
    """Extract it.each(…)(label, …) template strings from TS source.

    Handles all three JS/TS quote styles so that it.each(…)("label") and
    it.each(…)(`label`) forms are not silently skipped.
    """
    import re

    labels: set[str] = set()
    for m in re.finditer(
        r"""it\.each\b[\s\S]*?\)\s*\(\s*\n?\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)""",
        source,
    ):
        label = m.group(1) or m.group(2) or m.group(3)
        if label:
            labels.add(label)
    return labels


def _extract_ts_test_labels(source: str) -> set[str]:
    """Extract all test('...') / test("...") / test(`...`) labels from TS source.

    Handles all three JS/TS quote styles so that a test added as test("…") or
    test(`…`) is not silently missed by a single-quote-only regex.
    """
    import re

    labels: set[str] = set()
    for m in re.finditer(
        r"""^\s*test\s*\(\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)""",
        source,
        re.MULTILINE,
    ):
        label = m.group(1) or m.group(2) or m.group(3)
        if label:
            labels.add(label)
    return labels


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
            # TypeScript test suites use describe()/it()/test() with string labels.
            # Collect actual labels via the quote-aware helpers (all three JS/TS
            # quote styles) rather than a plain substring search — a substring match
            # would allow test_id "foo" to pass even if only "foobar" exists in
            # the file, silently weakening the anti-deletion guarantee.
            actual_labels = _extract_ts_it_labels(source)
            actual_labels |= _extract_ts_it_each_labels(source)
            # Also collect test('...') labels; some files use test() not it().
            for m in re.finditer(
                r"""^\s*test\s*\(\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)""",
                source,
                re.MULTILINE,
            ):
                label = m.group(1) or m.group(2) or m.group(3)
                if label:
                    actual_labels.add(label)
            for test_id in entry["test_ids"]:
                if test_id not in actual_labels:
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
        + "\n".join(f"  - {r}" for r in reintroduced)
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

    # Collect all it('label') / it("label") / it(`label`) test labels.
    # _extract_ts_it_labels handles all three JS/TS quote styles so that a
    # test written with double or backtick quotes is not silently missed.
    actual_labels: set[str] = _extract_ts_it_labels(source)
    # Collect it.each(...)('template') / it.each(...)("template") labels.
    actual_labels |= _extract_ts_it_each_labels(source)
    # Also collect test('...') / test("...") / test(`...`) labels — some suites
    # use test() instead of it(); missing this form lets a test() case escape
    # the manifest-completeness check entirely.
    actual_labels |= _extract_ts_test_labels(source)

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


def test_strategic_resident_lib_is_protected_with_all_tests():
    """The strategic-resident-lib agent test suite must appear in the manifest with all tests.

    agent/src/handlers/strategic-resident-lib.test.ts covers the core utility
    library for the conversational Workday resident session (config knobs, message
    cursor, dedup, content blocks, bootstrap/autonomous prompts, compaction
    checkpoint, exam ledger, operator-decision review, decisions register, surface
    catalog, tick-verdict parsing, and frame coalescer). Deleting or emptying it
    without operator sign-off must trip this guard. Verifies: (a) the file is in
    the protect section, (b) every it()/it.each() label in the file is listed, and
    (c) counts match so a silently-added test doesn't escape the manifest.
    """
    manifest = _load_manifest()
    target_path = "agent/src/handlers/strategic-resident-lib.test.ts"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the resident-lib "
        "test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")

    # Collect all it('label') / it("label") / it(`label`) test labels.
    # _extract_ts_it_labels handles all three JS/TS quote styles so that a
    # test written with double or backtick quotes is not silently missed.
    actual_labels: set[str] = _extract_ts_it_labels(source)
    # Collect it.each(...)('template') / it.each(...)("template") labels.
    actual_labels |= _extract_ts_it_each_labels(source)
    # Also collect test('...') / test("...") / test(`...`) labels — some suites
    # use test() instead of it(); missing this form lets a test() case escape
    # the manifest-completeness check entirely.
    actual_labels |= _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in strategic-resident-lib.test.ts but are NOT "
        "listed in the protect manifest. Add them so deletion of any resident-lib "
        "test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "strategic-resident-lib.test.ts. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_strategic_live_turn_is_protected_with_all_tests():
    """The StrategicLiveTurn component test suite must appear in the manifest with all tests.

    admin-ui/src/components/StrategicLiveTurn.test.tsx covers the
    StrategicResidentActivityWidget timer-truthfulness contract: the elapsed timer
    ticks only while a command is genuinely executing and freezes on idle or dead
    state. Deleting or emptying it without operator sign-off must trip this guard.
    Verifies: (a) the file is in the protect section, (b) every test() label in
    the file is listed, and (c) counts match.
    """
    manifest = _load_manifest()
    target_path = "admin-ui/src/components/StrategicLiveTurn.test.tsx"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the StrategicLiveTurn "
        "test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")

    # StrategicLiveTurn.test.tsx uses test('...') not it('...'). Use the
    # all-quote helper so double-quoted or backtick-quoted tests are not missed.
    actual_labels: set[str] = _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in StrategicLiveTurn.test.tsx but are NOT "
        "listed in the protect manifest. Add them so deletion of any "
        "StrategicLiveTurn test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "StrategicLiveTurn.test.tsx. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_strategic_live_turn_viewport_is_protected_with_all_tests():
    """The StrategicLiveTurn viewport test suite must appear in the manifest with all tests.

    admin-ui/src/components/StrategicLiveTurn.viewport.test.tsx covers viewport
    regression for StrategicLiveTurn components at 375px mobile width, guarding
    that streaming reply/reasoning and live activity strips remain contained.
    Deleting or emptying it without operator sign-off must trip this guard.
    Verifies: (a) the file is in the protect section, (b) every test() label in
    the file is listed, and (c) counts match.
    """
    manifest = _load_manifest()
    target_path = "admin-ui/src/components/StrategicLiveTurn.viewport.test.tsx"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the StrategicLiveTurn "
        "viewport test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")

    # StrategicLiveTurn.viewport.test.tsx uses test('...') not it('...'). Use
    # the all-quote helper so double-quoted or backtick-quoted tests are not missed.
    actual_labels: set[str] = _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in StrategicLiveTurn.viewport.test.tsx but are NOT "
        "listed in the protect manifest. Add them so deletion of any viewport "
        "regression test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "StrategicLiveTurn.viewport.test.tsx. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_use_strategic_liveness_is_protected_with_all_tests():
    """The useStrategicLiveness hook test suite must appear in the manifest with all tests.

    admin-ui/src/hooks/useStrategicLiveness.test.tsx covers the React hook that
    probes the backend liveness endpoint for the conversational Workday resident
    session (null/disabled short-circuit, mount fetch, stale-response generation
    guard, error-swallowing). Deleting or emptying it without operator sign-off
    must trip this guard. Verifies: (a) the file is in the protect section, (b)
    every it() label in the file is listed, and (c) counts match.
    """
    manifest = _load_manifest()
    target_path = "admin-ui/src/hooks/useStrategicLiveness.test.tsx"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the useStrategicLiveness "
        "test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")

    # useStrategicLiveness.test.tsx uses it('...'). Use the all-quote helper so
    # double-quoted or backtick-quoted tests are not silently missed.
    actual_labels: set[str] = _extract_ts_it_labels(source)
    actual_labels |= _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in useStrategicLiveness.test.tsx but are NOT "
        "listed in the protect manifest. Add them so deletion of any liveness "
        "hook test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "useStrategicLiveness.test.tsx. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


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
        "resident turn processing path."
    )


# ---------------------------------------------------------------------------
# Unit tests for _extract_ts_it_labels / _extract_ts_it_each_labels helpers
#
# These tests verify that the manifest-completeness guards collect test labels
# regardless of which JS/TS quote style is used. A single-quote-only regex
# would silently miss it("…") and it(`…`) forms, allowing a newly-added test
# to bypass the anti-deletion guarantee without any guard failure.
# ---------------------------------------------------------------------------


def test_ts_label_extraction_single_quoted():
    """_extract_ts_it_labels must collect single-quoted it() labels."""
    source = "  it('finds single-quoted label', () => { expect(1).toBe(1) })\n"
    labels = _extract_ts_it_labels(source)
    assert labels == {"finds single-quoted label"}, (
        f"Expected {{'finds single-quoted label'}}, got {labels!r}"
    )


def test_ts_label_extraction_double_quoted():
    """_extract_ts_it_labels must collect double-quoted it() labels.

    A regex that only matches single quotes silently misses this form, so a
    test added as it("new test") would escape the manifest-completeness check.
    """
    source = '  it("finds double-quoted label", () => { expect(1).toBe(1) })\n'
    labels = _extract_ts_it_labels(source)
    assert labels == {"finds double-quoted label"}, (
        f"Expected {{'finds double-quoted label'}}, got {labels!r}"
    )


def test_ts_label_extraction_backtick_quoted():
    """_extract_ts_it_labels must collect backtick-quoted it() labels.

    A regex that only matches single quotes silently misses this form, so a
    test added as it(`new test`) would escape the manifest-completeness check.
    """
    source = "  it(`finds backtick-quoted label`, () => { expect(1).toBe(1) })\n"
    labels = _extract_ts_it_labels(source)
    assert labels == {"finds backtick-quoted label"}, (
        f"Expected {{'finds backtick-quoted label'}}, got {labels!r}"
    )


def test_ts_label_extraction_mixed_quote_styles():
    """_extract_ts_it_labels collects labels from all three quote styles in one file."""
    source = (
        "  it('single-quoted', () => {})\n"
        '  it("double-quoted", () => {})\n'
        "  it(`backtick-quoted`, () => {})\n"
    )
    labels = _extract_ts_it_labels(source)
    assert labels == {"single-quoted", "double-quoted", "backtick-quoted"}, (
        f"Expected all three quote styles captured, got {labels!r}"
    )


def test_ts_label_extraction_single_quote_only_regex_misses_double_quoted():
    """Demonstrate the gap: old single-quote regex silently misses double-quoted labels.

    This test documents the negative case: using a single-quote-only pattern
    (the previous implementation) on a source that contains only a
    double-quoted it() call produces an empty set — no guard failure would
    fire for a manifest that does not list the label.  _extract_ts_it_labels
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
    )

    # The fixed helper correctly finds the label.
    fixed_result = _extract_ts_it_labels(source)
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
    )

    fixed_result = _extract_ts_it_labels(source)
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
        + "\n".join(f"  - {t}" for t in missing_tests)
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
        + "\n".join(f"  - {t}" for t in not_listed)
    )


def test_ts_it_each_label_extraction_double_quoted():
    """_extract_ts_it_each_labels must collect double-quoted it.each templates."""
    source = (
        "  it.each([\n"
        "    ['a'], ['b'],\n"
        "  ])(\"handles %s in double quotes\", (val) => { expect(val).toBeTruthy() })\n"
    )
    labels = _extract_ts_it_each_labels(source)
    assert labels == {"handles %s in double quotes"}, (
        f"Expected double-quoted it.each template captured, got {labels!r}"
    )


def test_ts_it_each_label_extraction_backtick():
    """_extract_ts_it_each_labels must collect backtick it.each templates."""
    source = (
        "  it.each([\n"
        "    ['a'], ['b'],\n"
        "  ])(`handles %s in backticks`, (val) => { expect(val).toBeTruthy() })\n"
    )
    labels = _extract_ts_it_each_labels(source)
    assert labels == {"handles %s in backticks"}, (
        f"Expected backtick it.each template captured, got {labels!r}"
    )


def test_ts_label_extraction_ignores_non_leading_it_calls():
    """_extract_ts_it_labels must not collect inline 'it' mentions, only leading it()."""
    source = (
        "  it('valid test label', () => {\n"
        "    // a comment mentioning it('inline') or describe it('as follows')\n"
        "    expect(doIt('something')).toBe(true)\n"
        "  })\n"
    )
    labels = _extract_ts_it_labels(source)
    # Only the first line should be collected; inline mentions must be ignored.
    assert labels == {"valid test label"}, (
        f"Expected only the leading it() to be collected, got {labels!r}"
    )


def test_ts_label_extraction_empty_source():
    """_extract_ts_it_labels returns an empty set for an empty source string."""
    assert _extract_ts_it_labels("") == set()
    assert _extract_ts_it_each_labels("") == set()


# ---------------------------------------------------------------------------
# Reverse completeness guards — artifact-materialization and tool-handoff suites
#
# These are TWO-WAY guards: (a) the file appears in the protect manifest with
# all its test_ids listed, AND (b) every actual test in the file is also in the
# manifest, so a silently-added-then-deleted test cannot escape detection.
# ---------------------------------------------------------------------------


def test_workday_artifact_materialization_is_protected_with_all_tests():
    """The artifact-materialization suite must appear in the manifest with all tests.

    backend/testing/test_workday_artifact_materialization.py covers exact
    Notes/Whiteboard materialization, immutable receipt validation, transient
    retry, permanent quarantine, authority/page fences, and bounded image
    delivery. Deleting or emptying it without operator sign-off must trip this
    guard. Verifies: (a) the file is in the protect section, (b) every
    def/async-def test function in the file is listed, and (c) counts match.
    """
    import re

    manifest = _load_manifest()
    target_path = "backend/testing/test_workday_artifact_materialization.py"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the artifact-materialization "
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
        "These test functions exist in test_workday_artifact_materialization.py but "
        "are NOT listed in the protect manifest. Add them so deletion of any "
        "artifact-materialization test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "test_workday_artifact_materialization.py. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_tool_resident_handoff_controls_is_protected_with_all_tests():
    """The ToolResidentHandoffControls test suite must appear in the manifest with all tests.

    admin-ui/src/components/ToolResidentHandoffControls.test.tsx covers the
    Notes/Whiteboard-to-conversation handoff controls that queue immutable
    revisions for the current Workday conversation. Deleting or emptying it
    without operator sign-off must trip this guard. Verifies: (a) the file is
    in the protect section, (b) every test() label in the file is listed, and
    (c) counts match.
    """
    manifest = _load_manifest()
    target_path = "admin-ui/src/components/ToolResidentHandoffControls.test.tsx"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the handoff controls "
        "test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")
    actual_labels: set[str] = _extract_ts_it_labels(source)
    actual_labels |= _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in ToolResidentHandoffControls.test.tsx but are NOT "
        "listed in the protect manifest. Add them so deletion of any handoff controls "
        "test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "ToolResidentHandoffControls.test.tsx. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_tool_resident_handoff_controls_viewport_is_protected_with_all_tests():
    """The ToolResidentHandoffControls viewport test suite must appear in the manifest.

    admin-ui/src/components/ToolResidentHandoffControls.viewport.test.tsx covers
    handoff controls at narrow operator viewports. Deleting or emptying it without
    operator sign-off must trip this guard. Verifies: (a) the file is in the
    protect section, (b) every test() label in the file is listed, and
    (c) counts match.
    """
    manifest = _load_manifest()
    target_path = "admin-ui/src/components/ToolResidentHandoffControls.viewport.test.tsx"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the handoff controls "
        "viewport test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")
    actual_labels: set[str] = _extract_ts_it_labels(source)
    actual_labels |= _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in ToolResidentHandoffControls.viewport.test.tsx but "
        "are NOT listed in the protect manifest. Add them so deletion of any "
        "handoff controls viewport test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "ToolResidentHandoffControls.viewport.test.tsx. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_resident_tool_handoff_util_is_protected_with_all_tests():
    """The residentToolHandoff utility test suite must appear in the manifest with all tests.

    admin-ui/src/utils/residentToolHandoff.test.ts covers durable actor/project-
    scoped handoff queue persistence, truthful failure, cross-tab wakeup,
    snapshot-safe acknowledgement, and expiry cleanup. Deleting or emptying it
    without operator sign-off must trip this guard. Verifies: (a) the file is in
    the protect section, (b) every it() label in the file is listed, and
    (c) counts match.
    """
    manifest = _load_manifest()
    target_path = "admin-ui/src/utils/residentToolHandoff.test.ts"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the resident tool handoff "
        "utility test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")
    actual_labels: set[str] = _extract_ts_it_labels(source)
    actual_labels |= _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in residentToolHandoff.test.ts but are NOT "
        "listed in the protect manifest. Add them so deletion of any resident "
        "tool handoff utility test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "residentToolHandoff.test.ts. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )


def test_pending_whiteboard_artifact_ref_is_protected_with_all_tests():
    """The pendingWhiteboardArtifactRef test suite must appear in the manifest with all tests.

    admin-ui/src/utils/pendingWhiteboardArtifactRef.test.ts covers crash/reload-
    safe Whiteboard promotion with exact receipt replay, coalescing, bounded retry
    pacing, and terminal intent preservation. Deleting or emptying it without
    operator sign-off must trip this guard. Verifies: (a) the file is in the
    protect section, (b) every test() label in the file is listed, and
    (c) counts match.
    """
    manifest = _load_manifest()
    target_path = "admin-ui/src/utils/pendingWhiteboardArtifactRef.test.ts"

    entries = [e for e in manifest["protect"] if e["path"] == target_path]
    assert entries, (
        f"{target_path} is not in the protect section of the manifest. "
        "Add it with all test_ids so silent deletion of the pending Whiteboard "
        "artifact ref test suite is caught at every CI run."
    )
    entry = entries[0]

    full_path = REPO_ROOT / target_path
    if not full_path.exists():
        pytest.skip(
            f"{target_path} not found — test_all_protect_paths_exist covers the missing-file case"
        )

    source = full_path.read_text(encoding="utf-8")
    actual_labels: set[str] = _extract_ts_it_labels(source)
    actual_labels |= _extract_ts_test_labels(source)

    listed_labels = set(entry["test_ids"])

    missing_from_manifest = sorted(actual_labels - listed_labels)
    extra_in_manifest = sorted(listed_labels - actual_labels)

    assert not missing_from_manifest, (
        "These test labels exist in pendingWhiteboardArtifactRef.test.ts but are NOT "
        "listed in the protect manifest. Add them so deletion of any pending "
        "Whiteboard artifact ref test is caught:\n"
        + "\n".join(f"  - {t}" for t in missing_from_manifest)
    )
    assert not extra_in_manifest, (
        "These test_ids are in the protect manifest but no longer exist in "
        "pendingWhiteboardArtifactRef.test.ts. Update the manifest:\n"
        + "\n".join(f"  - {t}" for t in extra_in_manifest)
    )
