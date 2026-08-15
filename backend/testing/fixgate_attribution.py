"""Shared, route-keyed attribution for differential test gates.

The publication gates may compare reports produced in different checkout roots
and by different harness mount layouts.  A traceback is diagnostic output, not
test identity: its paths, line numbers, counts, and assertion text are all
allowed to change while the same test remains a baseline failure.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FailureIdentity:
    """Stable identity of one failed JUnit node."""

    route: str
    testcase: str
    kind: str

    @property
    def test_key(self) -> str:
        return f"{self.route}::{self.testcase}"

    @property
    def key(self) -> str:
        return f"{self.test_key}::{self.kind}"


@dataclass
class JUnitReport:
    """The identity-bearing portion of a JUnit report."""

    failures: dict[str, str] = field(default_factory=dict)
    test_routes: set[str] = field(default_factory=set)
    skipped_routes: set[str] = field(default_factory=set)
    selectors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Attribution:
    """Route-keyed comparison between a current and baseline report."""

    new_failures: dict[str, str]
    suppressed_failures: dict[str, str]
    disappeared_failures: dict[str, str]
    skipped_failures: dict[str, str]


def _relative_source(source: Path, tree_root: Path) -> str:
    try:
        return source.relative_to(tree_root).as_posix()
    except ValueError:
        return source.as_posix()


def _resolve_route(
    classname: str, name: str, *, tree_root: Path | None
) -> tuple[str, str, str | None]:
    """Resolve pytest's classname/name into route, testcase, and source path."""

    parts = [part for part in classname.split(".") if part]
    if tree_root is not None:
        for start in range(len(parts)):
            for module_length in range(len(parts), start, -1):
                relative = Path(*parts[start:module_length]).with_suffix(".py")
                for source_root in (tree_root, tree_root / "backend"):
                    source = source_root / relative
                    if source.is_file():
                        suffix = [part for part in (*parts[module_length:], name) if part]
                        return (
                            _relative_source(source, tree_root),
                            "::".join(suffix),
                            str(source),
                        )

    # Backend JUnit output normally already has the stable testing.* route.  If
    # a report has no source tree available, preserve the structured route
    # rather than falling back to rendered failure details.
    return classname or "<collection>", name, None


def _parse_report_root(path: str | os.PathLike[str], *, label: str) -> ET.Element:
    report = Path(path)
    if not report.is_file():
        raise RuntimeError(f"{label} JUnit report is missing: {report}")
    if report.stat().st_size == 0:
        raise RuntimeError(f"{label} JUnit report is empty: {report}")
    try:
        root = ET.parse(report).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RuntimeError(f"{label} JUnit report is not parseable: {report}: {exc}") from exc
    if not any(element.tag == "testcase" for element in root.iter()):
        raise RuntimeError(f"{label} JUnit report has no testcase records: {report}")
    return root


def parse_junit_report(
    path: str | os.PathLike[str],
    *,
    tree_root: str | os.PathLike[str] | None = None,
    label: str = "report",
) -> JUnitReport:
    """Parse identity and status, ignoring diagnostic text for attribution."""

    root_path = Path(tree_root).resolve() if tree_root is not None else None
    parsed = JUnitReport()
    root = _parse_report_root(path, label=label)
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        route, testcase, source = _resolve_route(classname, name, tree_root=root_path)
        test_key = f"{route}::{testcase}"
        parsed.test_routes.add(test_key)
        if case.find("skipped") is not None:
            parsed.skipped_routes.add(test_key)
        detail_node = case.find("failure")
        kind = "failure"
        if detail_node is None:
            detail_node = case.find("error")
            kind = "error"
        if detail_node is None:
            continue
        identity = FailureIdentity(route=route, testcase=testcase, kind=kind)
        parsed.failures[identity.key] = (
            detail_node.text or detail_node.attrib.get("message", "")
        ).strip()
        if source is not None:
            suffix = f"::{testcase}" if testcase else ""
            source_path = Path(source)
            if root_path is not None:
                try:
                    selector_route = source_path.relative_to(root_path / "backend").as_posix()
                except ValueError:
                    selector_route = route
            else:
                selector_route = route
            parsed.selectors[identity.key] = f"{selector_route}{suffix}"
    return parsed


def parse_junit_failures(
    path: str | os.PathLike[str],
    *,
    tree_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Compatibility view containing only route-keyed failed nodes."""

    return parse_junit_report(path, tree_root=tree_root).failures


def _test_key(failure_key: str) -> str:
    # New keys always carry ``::<failure-kind>``. Direct callers from older
    # receipts may provide the historic route::test key; retaining it here is
    # deliberate compatibility for already-produced artifacts.
    parts = failure_key.rsplit("::", 1)
    return parts[0] if len(parts) == 2 and parts[1] in {"failure", "error"} else failure_key


def attribute_reports(current: JUnitReport, baseline: JUnitReport) -> Attribution:
    """Compare reports by route/test/kind, and account for missing routes."""

    current_keys = set(current.failures)
    baseline_keys = set(baseline.failures)
    suppressed = {
        key: current.failures[key] for key in sorted(current_keys & baseline_keys)
    }
    new = {
        key: current.failures[key] for key in sorted(current_keys - baseline_keys)
    }
    disappeared = {
        key: baseline.failures[key]
        for key in sorted(baseline_keys)
        if _test_key(key) not in current.test_routes
    }
    skipped = {
        key: baseline.failures[key]
        for key in sorted(baseline_keys)
        if _test_key(key) in current.skipped_routes
    }
    return Attribution(
        new_failures=new,
        suppressed_failures=suppressed,
        disappeared_failures=disappeared,
        skipped_failures=skipped,
    )


def _report_from_failures(failures: dict[str, str]) -> JUnitReport:
    return JUnitReport(
        failures=dict(failures),
        test_routes={_test_key(key) for key in failures},
    )


def attribute_failure_maps(
    current: dict[str, str], baseline: dict[str, str]
) -> Attribution:
    """Compatibility adapter for callers that already have failure maps."""

    return attribute_reports(_report_from_failures(current), _report_from_failures(baseline))


def classify_verification(
    *,
    runtime_available: bool,
    current_exit_code: int,
    current_failures: dict[str, str],
    baseline_failures: dict[str, str] | None = None,
    current_report: JUnitReport | None = None,
    baseline_report: JUnitReport | None = None,
) -> dict[str, object]:
    """Return a fail-closed publication verdict for a differential run."""

    if not runtime_available:
        return {"state": "runtime_unavailable", "blocking": True, "new_failures": {}}
    if baseline_failures is None and baseline_report is None:
        if current_exit_code == 0:
            return {"state": "passed", "blocking": False, "new_failures": {}}
        if not current_failures:
            return {"state": "execution_failure", "blocking": True, "new_failures": {}}
        return {
            "state": "attribution_unavailable",
            "blocking": True,
            "new_failures": current_failures,
        }

    current_report = current_report or _report_from_failures(current_failures)
    baseline_report = baseline_report or _report_from_failures(baseline_failures or {})
    if current_exit_code == 0 and not baseline_report.failures:
        return {
            "state": "passed",
            "blocking": False,
            "new_failures": {},
            "suppressed_failures": {},
            "disappeared_failures": {},
            "skipped_failures": {},
        }
    attributed = attribute_reports(current_report, baseline_report)
    new_failures = attributed.new_failures
    disappeared = attributed.disappeared_failures
    skipped = attributed.skipped_failures
    if new_failures:
        state = "test_failure"
    elif disappeared:
        state = "baseline_failure_disappeared"
    elif skipped:
        state = "baseline_failure_skipped"
    else:
        state = "preexisting_test_failure"
    return {
        "state": state,
        "blocking": bool(new_failures or disappeared or skipped),
        "new_failures": new_failures,
        "suppressed_failures": {
            key: {"detail": detail, "reason": "same route/test/failure kind failed on baseline"}
            for key, detail in attributed.suppressed_failures.items()
        },
        "disappeared_failures": {
            key: {"detail": detail, "reason": "baseline failing route absent from current report"}
            for key, detail in disappeared.items()
        },
        "skipped_failures": {
            key: {"detail": detail, "reason": "baseline failing route is skipped in current report"}
            for key, detail in skipped.items()
        },
    }


def extract_base(repo: Path, ref: str, destination: Path) -> None:
    """Safely extract a git archive, draining stderr before returning."""

    with tempfile.TemporaryFile() as stderr_file:
        archive = subprocess.Popen(
            ["git", "-c", f"safe.directory={repo}", "archive", ref],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert archive.stdout is not None
        try:
            with tarfile.open(fileobj=archive.stdout, mode="r|") as bundle:
                bundle.extractall(destination, filter="data")
        finally:
            return_code = archive.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read().decode("utf-8", errors="replace")
    if return_code != 0:
        raise RuntimeError(f"git archive {ref} failed ({return_code}): {stderr.strip()}")


__all__ = [
    "Attribution",
    "FailureIdentity",
    "JUnitReport",
    "attribute_failure_maps",
    "attribute_reports",
    "classify_verification",
    "extract_base",
    "parse_junit_failures",
    "parse_junit_report",
]
