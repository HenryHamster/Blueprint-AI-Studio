"""ALWAYS-ON corpus/shape guard for the no-CI gate's test-output classifier.

The agent-side ``classifyTestExecution`` (agent/src/utils/test-runners.ts) is the
sole producer of the ``detected=false`` verdict that lets the no-CI merge gate
fall open (route to proceed-without-checks). Its single most important safety
contract is *behavioral*: a genuine test suite whose output merely CONTAINS
placeholder-looking text — a failing test id like ``test_command_parser``, an
assertion body printing ``404 Not Found``, a cargo run with a filtered
zero-test submodule — must still classify as a real run (``ran``), so its real
pass/fail verdict is never silently downgraded to "no tests ran" and merged
UNVERIFIED.

That behavioral assertion runs in the agent vitest suite
(``classifier-fixtures.test.ts``), which executes the REAL classifier over the
shared fixture. But the agent vitest suite is NOT enforced in this repo's CI
(the ``agent`` CI job runs only ``tsc --noEmit``). So this Python guard — which
DOES run in plain GitHub CI — pins the *shape* of the shared corpus the vitest
net consumes, guaranteeing the representative corpus can never be silently
gutted (a category dropped, a polarity removed, a genuine-suite row relabelled)
while the structural adjacency invariant
(``test_classifier_missing_command_requires_adjacency``) stays green.

The fixture lives under ``backend/testing/__fixtures__/`` precisely so this
guard is always-on in plain GitHub CI; the agent vitest reads the very same
file via the established ``../../../backend/...`` URL convention, so both
consumers share ONE source of truth.

Fixture row schema (the shared contract, consumed by BOTH sides):
    { "name": str, "output": str, "expected": "ran"|"no_tests",
      "category": str, "genuine_suite": bool, "setup_expected"?: bool }
"""

import json
import os

import pytest

# The shared corpus covers both the no-tests classifier families and the setup-
# failure families. Every category MUST be represented so a future change
# cannot silently drop an entire behavior bucket from coverage.
EXPECTED_CATEGORIES = frozenset(
    {
        "command-not-found",
        "missing-script",
        "no-test-specified",
        "make-target",
        "zero-test",
        "setup-missing-runtime",
        "setup-missing-dependency",
    }
)

VALID_EXPECTED = frozenset({"ran", "no_tests"})

# Required row fields and their types. ``genuine_suite`` is the
# classifyTestExecution polarity flag:
# True  => the output must stay in the conservative ``ran`` bucket (real suite
#          OR setup failure that must not fall open to ``no_tests``).
# False => the output is a placeholder / empty / missing-runner signal (must
#          classify as ``no_tests``).
_REQUIRED_FIELDS = {
    "name": str,
    "output": str,
    "expected": str,
    "category": str,
    "genuine_suite": bool,
}


def _load_classifier_fixtures() -> list:
    """Load the shared classifier-output fixture (read-only, single source of truth)."""
    fixture_path = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__), "__fixtures__", "classifier_outputs.json"
        )
    )
    with open(fixture_path, encoding="utf-8") as fh:
        return json.load(fh)


CLASSIFIER_FIXTURES = _load_classifier_fixtures()


class TestClassifierOutputFixtureShape:
    """ALWAYS-ON: pin the shape of the shared classifier corpus so the agent-side
    behavioral net can never be silently emptied or skewed.
    """

    def test_fixture_is_nonempty(self):
        assert CLASSIFIER_FIXTURES, (
            "backend/testing/__fixtures__/classifier_outputs.json is empty — the "
            "behavioral regression corpus for classifyTestExecution was gutted. "
            "Restore representative pass/fail rows for every phrase category."
        )

    def test_every_row_has_valid_schema(self):
        """Each row must carry the full {name, output, expected, category,
        genuine_suite, setup_expected?} contract with correct types and a valid
        ``expected`` label. Both vitest and this guard rely on these field names."""
        errors = []
        seen_names = set()
        for idx, row in enumerate(CLASSIFIER_FIXTURES):
            if not isinstance(row, dict):
                errors.append(f"row {idx}: not an object")
                continue
            for field, ftype in _REQUIRED_FIELDS.items():
                if field not in row:
                    errors.append(f"row {idx}: missing field {field!r}")
                elif not isinstance(row[field], ftype):
                    errors.append(
                        f"row {idx} ({row.get('name', '?')}): field {field!r} must "
                        f"be {ftype.__name__}, got {type(row[field]).__name__}"
                    )
            name = row.get("name")
            if isinstance(name, str):
                if name in seen_names:
                    errors.append(f"row {idx}: duplicate name {name!r}")
                seen_names.add(name)
            if row.get("expected") not in VALID_EXPECTED:
                errors.append(
                    f"row {idx} ({row.get('name', '?')}): expected must be one of "
                    f"{sorted(VALID_EXPECTED)}, got {row.get('expected')!r}"
                )
            if "setup_expected" in row and not isinstance(row["setup_expected"], bool):
                errors.append(
                    f"row {idx} ({row.get('name', '?')}): setup_expected must be "
                    f"bool when present, got {type(row['setup_expected']).__name__}"
                )
            if not row.get("output"):
                errors.append(
                    f"row {idx} ({row.get('name', '?')}): output must be non-empty"
                )
        assert not errors, "classifier_outputs.json schema violations:\n" + "\n".join(
            errors
        )

    def test_all_categories_represented(self):
        """Every no-tests/setup-failure family must appear in the corpus."""
        present = {row["category"] for row in CLASSIFIER_FIXTURES}
        unknown = present - EXPECTED_CATEGORIES
        missing = EXPECTED_CATEGORIES - present
        assert not unknown, (
            f"classifier_outputs.json has unknown category/categories: {sorted(unknown)}. "
            f"Valid categories: {sorted(EXPECTED_CATEGORIES)}."
        )
        assert not missing, (
            f"classifier_outputs.json is missing category/categories: {sorted(missing)}. "
            "Every phrase family the classifier recognizes must keep a representative "
            "row so coverage cannot be silently dropped."
        )

    def test_both_polarities_present(self):
        """The corpus must carry BOTH a real-run polarity (genuine_suite=True =>
        'ran') and a placeholder polarity (genuine_suite=False => 'no_tests'),
        else the behavioral net would only protect one direction."""
        genuine = [r for r in CLASSIFIER_FIXTURES if r.get("genuine_suite") is True]
        placeholder = [
            r for r in CLASSIFIER_FIXTURES if r.get("genuine_suite") is False
        ]
        assert genuine, (
            "classifier_outputs.json has no genuine_suite=True rows — the corpus no "
            "longer protects the critical contract that a real suite printing "
            "placeholder-looking text still classifies as 'ran'."
        )
        assert placeholder, (
            "classifier_outputs.json has no genuine_suite=False rows — the corpus no "
            "longer protects the placeholder/empty => 'no_tests' direction."
        )

    def test_genuine_suite_rows_expect_ran(self):
        """The core safety invariant: every genuine real-run row must be labelled
        'ran'. A genuine_suite row labelled 'no_tests' would pin the very
        fall-open bug this corpus exists to prevent."""
        offenders = [
            row["name"]
            for row in CLASSIFIER_FIXTURES
            if row.get("genuine_suite") is True and row.get("expected") != "ran"
        ]
        assert not offenders, (
            "genuine_suite=True rows must expect 'ran' (a real run must never be "
            f"classified as no-tests): offending rows {offenders}."
        )

    def test_placeholder_rows_expect_no_tests(self):
        """Every placeholder/empty row must be labelled 'no_tests' — that is the
        whole point of the no-CI gate's proceed-without-checks fallback."""
        offenders = [
            row["name"]
            for row in CLASSIFIER_FIXTURES
            if row.get("genuine_suite") is False and row.get("expected") != "no_tests"
        ]
        assert not offenders, (
            "genuine_suite=False rows must expect 'no_tests' (a placeholder/empty "
            f"signal): offending rows {offenders}."
        )

    def test_command_not_found_has_both_polarities(self):
        """The command-not-found family is the one the structural adjacency
        invariant hardened; it MUST keep both a placeholder row (missing runner =>
        no_tests) and a genuine row (failure whose output merely contains the
        signal words non-adjacently => ran), so the behavioral net pins exactly
        the contract that source-scan cannot verify."""
        rows = [
            r for r in CLASSIFIER_FIXTURES if r.get("category") == "command-not-found"
        ]
        polarities = {r.get("genuine_suite") for r in rows}
        assert {True, False} <= polarities, (
            "command-not-found category must keep BOTH a genuine_suite=True row "
            "(genuine failure containing 'command'/'not found' as data => ran) and "
            "a genuine_suite=False row (real missing-runner => no_tests). Present "
            f"polarities: {polarities}."
        )

    def test_zero_test_has_both_polarities(self):
        """The zero-test family (Rust count-guard / Go pass-detection / pytest) is
        the other behaviorally-hardened category; keep both polarities so a real
        suite with a filtered zero-test submodule is pinned to 'ran'."""
        rows = [r for r in CLASSIFIER_FIXTURES if r.get("category") == "zero-test"]
        polarities = {r.get("genuine_suite") for r in rows}
        assert {True, False} <= polarities, (
            "zero-test category must keep BOTH a genuine_suite=True row (real suite "
            "whose output contains a zero-test summary as data => ran) and a "
            f"genuine_suite=False row (truly empty suite => no_tests). Present "
            f"polarities: {polarities}."
        )

    def test_setup_classifier_rows_exist(self):
        """The shared corpus must carry setup-failure rows so the agent-side
        setup classifier stays covered alongside classifyTestExecution."""
        setup_rows = [r for r in CLASSIFIER_FIXTURES if "setup_expected" in r]
        assert setup_rows, (
            "classifier_outputs.json has no setup_expected rows — the shared "
            "setup-failure regression corpus was gutted."
        )
        positives = [r["name"] for r in setup_rows if r["setup_expected"] is True]
        negatives = [r["name"] for r in setup_rows if r["setup_expected"] is False]
        assert positives, (
            "classifier_outputs.json has no setup_expected=true rows — missing "
            "runtime/dependency setup failures are no longer represented."
        )
        assert negatives, (
            "classifier_outputs.json has no setup_expected=false rows — the "
            "shared corpus no longer carries a negative control against "
            "setup-classifier false positives."
        )


def test_load_helper_raises_on_missing_file(tmp_path, monkeypatch):
    """Defensive: the loader must fail loudly (not silently pass with an empty
    corpus) if the fixture file disappears."""
    with pytest.raises(FileNotFoundError):
        with open(
            os.path.join(str(tmp_path), "does_not_exist.json"), encoding="utf-8"
        ) as fh:
            json.load(fh)
