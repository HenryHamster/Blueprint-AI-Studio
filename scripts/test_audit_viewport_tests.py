"""Regression coverage for the viewport test identity inventory."""

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).with_name("audit-viewport-tests.py")
_SPEC = importlib.util.spec_from_file_location("audit_viewport_tests", _SCRIPT)
assert _SPEC and _SPEC.loader
audit_viewport_tests = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit_viewport_tests)


def test_scan_file_counts_backtick_and_parameterized_declarations(tmp_path: Path):
    """Viewport inventory shares Workday's backtick and ``.each`` identities."""
    viewport_test = tmp_path / "Example.viewport.test.tsx"
    viewport_test.write_text(
        "it(`backtick label`, () => { expect(true).toBe(true) })\n"
        "test.each([[1]])('parameterized %s', () => { expect(true).toBe(true) })\n",
        encoding="utf-8",
    )

    report = audit_viewport_tests.scan_file(viewport_test)

    assert report["stats"]["total_tests"] == 2
