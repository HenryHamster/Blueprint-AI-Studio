"""Shared, route-keyed attribution for differential test gates.

The publication gates may compare reports produced in different checkout roots
and by different harness mount layouts.  A traceback is diagnostic output, but
meaningful changes to its failure detail must remain attributable to the task;
only known checkout- and runtime-specific noise is normalized.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


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
    offender_sets: dict[str, frozenset[str]] = field(default_factory=dict)
    # Keep the original occurrence fields as well as the key-indexed maps.
    # The Node merge verifier must hand records to this module's structured
    # comparator without reimplementing pytest's JUnit route resolution.
    structured_records: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class Attribution:
    """Route-keyed comparison between a current and baseline report."""

    new_failures: dict[str, str]
    suppressed_failures: dict[str, str]
    disappeared_failures: dict[str, str]
    skipped_failures: dict[str, str]


# The structured boundary is used by callers that cannot safely duplicate the
# parser's normalization rules (notably the Node merge-time verifier).  Keep
# both sides bounded so a malformed or accidentally unbounded reporter cannot
# turn the comparator into an untrusted memory sink.
MAX_STRUCTURED_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_STRUCTURED_RECORDS = 4096
MAX_STRUCTURED_DETAIL_LENGTH = 32_768
MAX_STRUCTURED_FIELD_LENGTH = 1024
MAX_STRUCTURED_OFFENDERS = 256
MAX_STRUCTURED_OFFENDER_LENGTH = 512


def _relative_source(source: Path, tree_root: Path) -> str:
    try:
        return source.relative_to(tree_root).as_posix()
    except ValueError:
        return source.as_posix()


def _normalize_failure_detail(detail: str, *, tree_root: Path | None) -> str:
    """Remove checkout- and runtime-specific noise from failure diagnostics."""

    # pytest-asyncio names its internal Tasks 'Task-<n>' with a process-wide,
    # monotonically increasing counter. The same test can surface a different
    # counter value on every run (baseline vs current, or even rerun vs rerun)
    # purely because of how many async Tasks happened to be created earlier in
    # the session — never because the test's own behavior changed. Left
    # unnormalized, this alone makes an identical pre-existing failure
    # unmatchable across a baseline/current comparison.
    normalized = re.sub(r"Task-\d+", "Task-<n>", detail)
    # A coroutine/Task repr truncates its embedded source path with a leading
    # '...' once the qualname exceeds Python's internal repr budget, so the
    # checkout-root replace below (which only matches the FULL, untruncated
    # path) never fires on it. The checkout directory name differs between
    # the current tree and a freshly `git archive`-extracted baseline temp
    # dir, so this truncated prefix is exactly the same kind of checkout
    # noise the full-path replace targets — just past the ellipsis instead of
    # at the start of the string.
    normalized = re.sub(r"\.\.\.[^\s>]*?/backend/", ".../<repo-root>/backend/", normalized)
    # A canonical UUID (e.g. a task id embedded in a diagnostic message by
    # f"... for task {task_id_str}: {chain_exc}") is generated fresh on every
    # run. It is run-unique by construction, never diff-sensitive, so an
    # identical pre-existing failure must not be defeated by it — normalize it
    # the same way as the other run-unique tokens above.
    normalized = re.sub(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "<uuid>",
        normalized,
    )
    if tree_root is None:
        return normalized
    normalized = normalized.replace(str(tree_root), "<tree-root>")
    normalized = re.sub(
        r"/tmp/pytest-of-[^/]+/pytest-\d+",
        "<pytest-temp>",
        normalized,
    )
    return re.sub(r"(object at )0x[0-9a-fA-F]+", r"\1<address>", normalized)


_OFFENDER_SET_RE = re.compile(
    r"(?m)^[ \t]*BLUEPRINT_OFFENDERS:[ \t]*(\[[^\r\n]*\])[ \t]*$"
)


def format_offender_marker(offenders: Iterable[str]) -> str:
    """Render a deterministic, machine-readable identity set for a guard.

    Guard diagnostics remain written for humans, but their rendered prose often
    includes counts, ordering, and other context that can legitimately change
    while the underlying failure is inherited.  Appending this marker gives the
    differential gate a stable identity without weakening its route, testcase,
    or failure-kind checks.
    """

    values = list(offenders)
    if not all(isinstance(value, str) and value and "\n" not in value for value in values):
        raise ValueError("offender identities must be non-empty single-line strings")
    return "BLUEPRINT_OFFENDERS: " + json.dumps(
        sorted(set(values)), separators=(",", ":")
    )


def _extract_offender_set(detail: str) -> frozenset[str] | None:
    """Read the canonical offender marker, rejecting malformed diagnostics."""

    match = _OFFENDER_SET_RE.search(detail)
    if match is None:
        return None
    try:
        values = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    if len(values) != len(set(values)):
        return None
    return frozenset(values)


def _append_failure(
    report: JUnitReport,
    identity: FailureIdentity,
    detail: str,
    *,
    offenders: frozenset[str] | None = None,
) -> str:
    """Add one failure while retaining repeated records as occurrences."""

    occurrence = 1
    key = identity.key
    while key in report.failures:
        occurrence += 1
        key = f"{identity.key}::occurrence={occurrence}"
    report.failures[key] = detail
    if offenders is not None:
        report.offender_sets[key] = offenders
    return key


def _resolve_route(
    classname: str, name: str, *, tree_root: Path | None
) -> tuple[str, str, str | None]:
    """Resolve pytest's classname/name into route, testcase, and source path."""

    parts = [part for part in classname.split(".") if part]
    node = name
    if not parts:
        # pytest's collection-error JUnit shape puts the dotted module path in
        # ``name`` and leaves ``classname`` empty.
        parts = [part for part in name.split(".") if part]
        node = ""
    if tree_root is not None:
        for start in range(len(parts)):
            for module_length in range(len(parts), start, -1):
                relative = Path(*parts[start:module_length]).with_suffix(".py")
                for source_root in (tree_root, tree_root / "backend"):
                    source = source_root / relative
                    if source.is_file():
                        suffix = [part for part in (*parts[module_length:], node) if part]
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
    """Parse identity and status, normalizing only known diagnostic noise."""

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
        detail = _normalize_failure_detail(
            (detail_node.text or detail_node.attrib.get("message", "")).strip(),
            tree_root=root_path,
        )
        _append_failure(parsed, identity, detail, offenders=_extract_offender_set(detail))
        record: dict[str, object] = {
            "route": route,
            "testcase": testcase or "<collection>",
            "kind": kind,
            "detail": detail,
        }
        offenders = _extract_offender_set(detail)
        if offenders is not None:
            record["offenders"] = sorted(offenders)
        parsed.structured_records.append(record)
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
    # deliberate compatibility for already-produced artifacts. Repeated
    # diagnostics append an occurrence discriminator *after* the failure kind,
    # but coverage remains keyed by the shared route/testcase identity.
    parts = failure_key.rsplit("::", 1)
    if len(parts) == 2 and re.fullmatch(r"occurrence=[1-9]\d*", parts[1]):
        failure_key = parts[0]
    parts = failure_key.rsplit("::", 1)
    return parts[0] if len(parts) == 2 and parts[1] in {"failure", "error"} else failure_key


def _report_offender_set(report: JUnitReport, key: str) -> frozenset[str] | None:
    """Return explicit or embedded offender identity for compatibility callers."""

    if key in report.offender_sets:
        return report.offender_sets[key]
    return _extract_offender_set(report.failures.get(key, ""))


def attribute_reports(current: JUnitReport, baseline: JUnitReport) -> Attribution:
    """Compare reports by route/test/kind and normalized failure detail."""

    current_keys = set(current.failures)
    baseline_keys = set(baseline.failures)
    shared = current_keys & baseline_keys
    suppressed = {}
    for key in sorted(shared):
        current_offenders = _report_offender_set(current, key)
        baseline_offenders = _report_offender_set(baseline, key)
        if current_offenders is not None and baseline_offenders is not None:
            is_preexisting = current_offenders <= baseline_offenders
        else:
            is_preexisting = current.failures[key] == baseline.failures[key]
        if is_preexisting:
            suppressed[key] = current.failures[key]
    new = {
        key: current.failures[key] for key in sorted(current_keys - baseline_keys)
    }
    new.update(
        {
            key: current.failures[key]
            for key in sorted(shared - set(suppressed))
        }
    )
    disappeared = {
        key: baseline.failures[key]
        for key in sorted(baseline_keys)
        if key not in current.failures and _test_key(key) not in current.skipped_routes
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


def _structured_string(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(
            f"structured {field_name} must be a non-empty string of at most {max_length} characters"
        )
    if "\n" in value or "\r" in value:
        raise ValueError(f"structured {field_name} must be a single-line string")
    return value


def _structured_report(records: object, *, label: str) -> JUnitReport:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise ValueError(f"structured {label} must be an occurrence array")
    if len(records) > MAX_STRUCTURED_RECORDS:
        raise ValueError(
            f"structured {label} exceeds the {MAX_STRUCTURED_RECORDS}-record limit"
        )

    report = JUnitReport()
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, Mapping):
            raise ValueError(f"structured {label}[{index}] must be an object")
        allowed = {"route", "testcase", "kind", "detail", "offenders"}
        unknown = set(raw_record) - allowed
        if unknown:
            raise ValueError(
                f"structured {label}[{index}] has unsupported fields: {sorted(unknown)}"
            )
        route = _structured_string(
            raw_record.get("route"),
            field_name=f"{label}[{index}].route",
            max_length=MAX_STRUCTURED_FIELD_LENGTH,
        )
        testcase = _structured_string(
            raw_record.get("testcase"),
            field_name=f"{label}[{index}].testcase",
            max_length=MAX_STRUCTURED_FIELD_LENGTH,
        )
        kind = _structured_string(
            raw_record.get("kind"),
            field_name=f"{label}[{index}].kind",
            max_length=16,
        )
        if kind not in {"failure", "error"}:
            raise ValueError(f"structured {label}[{index}].kind must be failure or error")
        detail = raw_record.get("detail")
        if not isinstance(detail, str) or len(detail) > MAX_STRUCTURED_DETAIL_LENGTH:
            raise ValueError(
                f"structured {label}[{index}].detail must be a string of at most "
                f"{MAX_STRUCTURED_DETAIL_LENGTH} characters"
            )

        offenders: frozenset[str] | None = None
        if "offenders" in raw_record:
            raw_offenders = raw_record["offenders"]
            if (
                not isinstance(raw_offenders, Sequence)
                or isinstance(raw_offenders, (str, bytes, bytearray))
                or len(raw_offenders) > MAX_STRUCTURED_OFFENDERS
            ):
                raise ValueError(
                    f"structured {label}[{index}].offenders must contain at most "
                    f"{MAX_STRUCTURED_OFFENDERS} identities"
                )
            values = [
                _structured_string(
                    value,
                    field_name=f"{label}[{index}].offenders[{offender_index}]",
                    max_length=MAX_STRUCTURED_OFFENDER_LENGTH,
                )
                for offender_index, value in enumerate(raw_offenders)
            ]
            if len(values) != len(set(values)):
                raise ValueError(f"structured {label}[{index}].offenders must be unique")
            offenders = frozenset(values)

        identity = FailureIdentity(route=route, testcase=testcase, kind=kind)
        normalized = _normalize_failure_detail(detail, tree_root=None)
        _append_failure(report, identity, normalized, offenders=offenders)
        report.test_routes.add(identity.test_key)
    return report


def compare_structured(payload: Mapping[str, object]) -> dict[str, object]:
    """Compare bounded occurrence arrays using the canonical normalization.

    The boundary intentionally accepts only ``current`` and ``baseline``
    arrays. Each occurrence contains ``route``, ``testcase``, ``kind`` and
    ``detail``, with an optional offender set. The returned object is JSON-safe
    and uses the same route/kind/detail/offender semantics as publication
    attribution. Invalid or oversized input raises ``ValueError`` so a caller
    can fail closed rather than silently falling back to test-name matching.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("structured attribution payload must be an object")
    if set(payload) != {"current", "baseline"}:
        raise ValueError("structured attribution payload must contain only current and baseline")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"structured attribution payload is not JSON-safe: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_STRUCTURED_PAYLOAD_BYTES:
        raise ValueError(
            f"structured attribution payload exceeds the {MAX_STRUCTURED_PAYLOAD_BYTES}-byte limit"
        )

    current = _structured_report(payload["current"], label="current")
    baseline = _structured_report(payload["baseline"], label="baseline")
    attributed = attribute_reports(current, baseline)
    result: dict[str, object] = {
        "blocking": bool(attributed.new_failures),
        "new_failures": attributed.new_failures,
        "suppressed_failures": {
            key: {"detail": detail, "reason": "same route/test/failure kind failed on baseline"}
            for key, detail in attributed.suppressed_failures.items()
        },
        "disappeared_failures": attributed.disappeared_failures,
        "skipped_failures": attributed.skipped_failures,
    }
    try:
        output_size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - internal types are JSON-safe
        raise ValueError(f"structured attribution output is not JSON-safe: {exc}") from exc
    if output_size > MAX_STRUCTURED_PAYLOAD_BYTES:
        raise ValueError("structured attribution output exceeds the payload limit")
    return result


def _structured_cli_error(message: str) -> dict[str, object]:
    return {"blocking": True, "error": {"code": "invalid_input", "message": message}}


def _run_structured_cli() -> int:
    raw = sys.stdin.buffer.read(MAX_STRUCTURED_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_STRUCTURED_PAYLOAD_BYTES:
        result = _structured_cli_error(
            f"structured attribution payload exceeds the {MAX_STRUCTURED_PAYLOAD_BYTES}-byte limit"
        )
        print(json.dumps(result, separators=(",", ":")))
        return 2
    try:
        payload = json.loads(raw.decode("utf-8"))
        result = compare_structured(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps(_structured_cli_error(str(exc)), separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


def _run_junit_records_cli(report_path: str, tree_root: str) -> int:
    """Emit canonical structured occurrences for a JUnit-producing runner.

    This intentionally reuses ``parse_junit_report``: callers must not copy
    route resolution or diagnostic normalization into another language merely
    to reach the structured comparator.
    """

    try:
        report = parse_junit_report(report_path, tree_root=tree_root, label="merge test")
        print(json.dumps(report.structured_records, ensure_ascii=False, separators=(",", ":")))
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")))
        return 2
    return 0


def test_route_coverage_gaps(current: JUnitReport, baseline: JUnitReport) -> dict[str, str]:
    """Return independently observable test-route coverage losses.

    Differential failure attribution intentionally permits a baseline failure
    to resolve. A lane that changes its executable test scope must therefore
    separately prove that it still executed each relevant baseline route. A
    missing route or a route now reported as skipped is a coverage regression,
    not a reclassification of the inherited failure identity.
    """

    # Scope the obligation to routes that actually carried a baseline failure.
    # A lane may freely rename, drop, or reparametrize a route that was green at
    # base; only an inherited failure identity must still be observably executed,
    # otherwise every legitimate test rename would publish a coverage_regression.
    obligated = {_test_key(key) for key in baseline.failures} & baseline.test_routes
    missing = obligated - current.test_routes
    skipped = obligated & current.skipped_routes
    gaps = dict.fromkeys(
        sorted(missing), "baseline test route is absent from the current report"
    )
    gaps.update(
        dict.fromkeys(
            sorted(skipped), "baseline test route is skipped in the current report"
        )
    )
    return gaps


def is_attributable_pytest_result(exit_code: int, report: JUnitReport) -> bool:
    """Return whether pytest's exit code and JUnit failures agree."""

    has_failures = bool(report.failures)
    return (exit_code == 0 and not has_failures) or (exit_code == 1 and has_failures)


def classify_verification(
    *,
    runtime_available: bool,
    current_exit_code: int,
    current_report: JUnitReport,
    baseline_report: JUnitReport | None = None,
) -> dict[str, object]:
    """Return a verdict from lossless JUnit reports, failing closed on no base."""

    if not runtime_available:
        return {"state": "runtime_unavailable", "blocking": True, "new_failures": {}}

    if not is_attributable_pytest_result(current_exit_code, current_report):
        return {"state": "execution_failure", "blocking": True, "new_failures": {}}

    if baseline_report is None:
        if current_exit_code == 0:
            return {"state": "passed", "blocking": False, "new_failures": {}}
        return {
            "state": "attribution_unavailable",
            "blocking": True,
            "new_failures": current_report.failures,
        }

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
        # Resolved or skipped inherited identities remain receipt evidence.
        # Lanes with mutable test scope enforce route coverage separately;
        # publication attribution itself blocks only introduced identities.
        "blocking": bool(new_failures),
        "new_failures": new_failures,
        "suppressed_failures": {
            key: {"detail": detail, "reason": "same route/test/failure kind failed on baseline"}
            for key, detail in attributed.suppressed_failures.items()
        },
        "disappeared_failures": {
            key: {
                "detail": detail,
                "reason": (
                    "baseline failure no longer reproduces in current report"
                    if _test_key(key) in current_report.test_routes
                    else "baseline failing route absent from current report"
                ),
            }
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
    "attribute_reports",
    "classify_verification",
    "compare_structured",
    "extract_base",
    "format_offender_marker",
    "is_attributable_pytest_result",
    "parse_junit_failures",
    "parse_junit_report",
]


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--junit-records":
        raise SystemExit(_run_junit_records_cli(sys.argv[2], sys.argv[3]))
    raise SystemExit(_run_structured_cli())
