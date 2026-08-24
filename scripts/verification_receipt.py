#!/usr/bin/env python3
"""One durable, multi-lane record of what each attributed gate lane suppressed.

The local publication gate runs several baseline-attributed lanes -- the
targeted backend pytest lane, the standalone static corpus, the Vitest suites
and the TypeScript compiles.  Every one of them already computes a structured
attribution through ``backend/testing/fixgate_attribution.py``, but only the
backend lane used to leave durable evidence; the rest printed their suppressed
baseline failures to stdout, where they died with the VM.

This module is the single writer for that sidecar.  Each lane MERGES its own
block into ``/tmp/project_verification_receipt_<task-id>.json`` under a stable
lane identity, so lanes never collide and re-running a lane replaces rather
than duplicates its block.  The pre-existing top-level ``pytest`` block is
preserved exactly where and as it is -- ``agent/src/prompts/pr-helper.ts``
reads it today.

HARD CONSTRAINT: writing a receipt must never change a lane's verdict.
``record_lane`` therefore swallows every failure, warns, and returns False.
A lane that cannot resolve a receipt path simply does not write one.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping

RECEIPT_VERSION = 1

# Where ./check and the generated gate both put the sidecar.
RECEIPT_DIRECTORY = Path("/tmp")
RECEIPT_PREFIX = "project_verification_receipt_"

# The gate resolves the task identity once (TASK_ID, falling back to its own
# generated id or ``local-developer``) and exports the resolved path here, so a
# guard never has to re-derive an identity and disagree with its caller.
RECEIPT_PATH_ENV = "BLUEPRINT_VERIFICATION_RECEIPT"

# Stable lane identities. `<producer>` or `<producer>:<suite>`; the suite half
# keeps two lanes of the same producer from colliding on a shared identity.
LANE_BACKEND_PYTEST = "backend-pytest"
LANE_STATIC_CORPUS = "static-corpus"

FAILURE_MAP_KEYS = (
    "new_failures",
    "suppressed_failures",
    "disappeared_failures",
    "skipped_failures",
)

# Bounds mirroring backend/app/task_blocked.py, so a lane block that reaches the
# durable publication receipt is already within what that endpoint accepts.
MAX_IDENTITIES_PER_MAP = 100
MAX_IDENTITY_CHARS = 512
MAX_DETAIL_CHARS = 4000

_LANE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(:[a-z0-9][a-z0-9._-]*)?$")


def vitest_lane(suite: str) -> str:
    """Return the receipt lane identity for a Vitest suite."""

    return f"vitest:{suite}"


def typecheck_lane(suite: str) -> str:
    """Return the receipt lane identity for a TypeScript compile suite."""

    return f"typecheck:{suite}"


def resolve_receipt_path(env: Mapping[str, str] | None = None) -> Path | None:
    """Return the sidecar path for this run, or None when there is no identity.

    A missing identity means "do not write" -- never "fail".  An explicitly
    empty ``BLUEPRINT_VERIFICATION_RECEIPT`` is an opt-out.
    """

    environment = os.environ if env is None else env
    override = environment.get(RECEIPT_PATH_ENV)
    if override is not None:
        stripped = override.strip()
        return Path(stripped) if stripped else None
    task_id = (environment.get("TASK_ID") or "").strip()
    if not task_id:
        return None
    return RECEIPT_DIRECTORY / f"{RECEIPT_PREFIX}{task_id}.json"


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def normalize_failure_map(mapping: Mapping[str, Any] | None) -> dict[str, dict[str, str]]:
    """Return a bounded ``identity -> {detail, reason}`` map of structured ids.

    Accepts both attribution shapes in the tree: the raw ``identity -> detail``
    maps returned by ``attribute_reports`` and the already-annotated
    ``identity -> {"detail", "reason"}`` maps returned by
    ``classify_verification``.
    """

    if not isinstance(mapping, Mapping):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for key in sorted(mapping)[:MAX_IDENTITIES_PER_MAP]:
        value = mapping[key]
        if isinstance(value, Mapping):
            detail = str(value.get("detail", ""))
            reason = str(value.get("reason", ""))
        else:
            detail = str(value)
            reason = ""
        entry = {"detail": _truncate(detail, MAX_DETAIL_CHARS)}
        if reason:
            entry["reason"] = _truncate(reason, MAX_DETAIL_CHARS)
        normalized[_truncate(str(key), MAX_IDENTITY_CHARS)] = entry
    return normalized


def build_lane_block(
    lane: str,
    *,
    state: str,
    blocking: bool = False,
    new_failures: Mapping[str, Any] | None = None,
    suppressed_failures: Mapping[str, Any] | None = None,
    disappeared_failures: Mapping[str, Any] | None = None,
    skipped_failures: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one lane's structured block, ready to merge into the sidecar."""

    if not _LANE_RE.match(lane):
        raise ValueError(f"invalid receipt lane identity: {lane!r}")
    block: dict[str, Any] = {
        "lane": lane,
        "state": str(state),
        "blocking": bool(blocking),
        "new_failures": normalize_failure_map(new_failures),
        "suppressed_failures": normalize_failure_map(suppressed_failures),
        "disappeared_failures": normalize_failure_map(disappeared_failures),
        "skipped_failures": normalize_failure_map(skipped_failures),
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if extra:
        for key, value in extra.items():
            if key not in block:
                block[key] = value
    return block


def _load_receipt(path: Path) -> dict[str, Any]:
    """Return the existing receipt, treating an unusable one as absent.

    A malformed sidecar must not make the merging lane fail: the pre-merge
    behaviour was a wholesale overwrite, and this keeps that floor.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def merge_receipt(
    path: Path,
    *,
    lane: str | None = None,
    block: Mapping[str, Any] | None = None,
    top_level: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a lane block and/or top-level keys into the sidecar atomically.

    Returns the merged payload.  Raises only on an unwritable destination.
    """

    payload = _load_receipt(path)
    payload["version"] = RECEIPT_VERSION
    if top_level:
        payload.update(top_level)
    if lane is not None:
        if block is None:
            raise ValueError("merge_receipt requires a block when a lane is given")
        lanes = payload.get("lanes")
        if not isinstance(lanes, dict):
            lanes = {}
        # Replace, never append: a re-run of the same lane supersedes itself.
        lanes[lane] = dict(block)
        payload["lanes"] = lanes
    _atomic_write(path, payload)
    return payload


def record_lane(
    lane: str,
    *,
    state: str,
    blocking: bool = False,
    new_failures: Mapping[str, Any] | None = None,
    suppressed_failures: Mapping[str, Any] | None = None,
    disappeared_failures: Mapping[str, Any] | None = None,
    skipped_failures: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    receipt_path: Path | None = None,
) -> bool:
    """Record one lane's attribution, never changing that lane's verdict.

    Returns whether a block was written.  Every failure -- no identity, no
    filesystem, malformed inputs -- warns and returns False.
    """

    try:
        path = receipt_path if receipt_path is not None else resolve_receipt_path(env)
        if path is None:
            return False
        merge_receipt(
            path,
            lane=lane,
            block=build_lane_block(
                lane,
                state=state,
                blocking=blocking,
                new_failures=new_failures,
                suppressed_failures=suppressed_failures,
                disappeared_failures=disappeared_failures,
                skipped_failures=skipped_failures,
                extra=extra,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a receipt never decides a verdict
        print(
            f"[verification-receipt] WARN could not record lane {lane}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def record_attribution(
    lane: str,
    attribution: Any,
    *,
    state: str,
    blocking: bool = False,
    extra: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    receipt_path: Path | None = None,
) -> bool:
    """Record an ``Attribution`` from the shared comparator for one lane."""

    return record_lane(
        lane,
        state=state,
        blocking=blocking,
        new_failures=getattr(attribution, "new_failures", None),
        suppressed_failures=getattr(attribution, "suppressed_failures", None),
        disappeared_failures=getattr(attribution, "disappeared_failures", None),
        skipped_failures=getattr(attribution, "skipped_failures", None),
        extra=extra,
        env=env,
        receipt_path=receipt_path,
    )


def _junit_counts(junit: Path) -> tuple[int, int, int]:
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        # A blocking attribution result is still useful when the runner did
        # not leave a parseable JUnit file.  The attribution maps and state are
        # the durable diagnosis; zero counts make the missing report explicit
        # without preventing that diagnosis from being recorded.
        return 0, 0, 0
    cases = list(root.iter("testcase"))
    skipped = sum(1 for case in cases if case.find("skipped") is not None)
    failed = sum(
        1
        for case in cases
        if case.find("failure") is not None or case.find("error") is not None
    )
    return len(cases), skipped, failed


def _backend_pytest_block(
    *,
    harness: str,
    selectors: Iterable[str],
    attribution: Mapping[str, Any],
    junit: Path,
) -> dict[str, Any]:
    """Return the legacy top-level ``pytest`` block, unchanged in shape.

    ``agent/src/prompts/pr-helper.ts`` reads exactly these keys today; the
    per-lane structure is additive and lives beside it.
    """

    tests, skipped, failed = _junit_counts(junit)
    blocking = bool(attribution.get("blocking", False))
    current_exit_code = int(attribution.get("current_exit_code", 0))
    # ``exit_code`` was historically hard-coded to zero because this writer
    # only ran on the passing branch.  Once blocking diagnostics are durable,
    # a consumer must be able to reject this block on its own: a blocking
    # result with a clean runner exit (for example route-coverage regression)
    # still gets a nonzero credential value.
    receipt_exit_code = current_exit_code if current_exit_code != 0 else (1 if blocking else 0)
    return {
        "command": f"USE_TEST_POSTGRES=true bash {harness} <impacted-selectors>",
        "harness": harness,
        "selectors": list(selectors),
        "targeted": True,
        "use_test_postgres": True,
        "postgres_sidecar": True,
        "state": attribution["state"],
        "current_exit_code": current_exit_code,
        "tests": tests,
        "skipped": skipped,
        "failures": failed,
        "new_failures": attribution.get("new_failures", {}),
        "suppressed_failures": attribution.get("suppressed_failures", {}),
        "disappeared_failures": attribution.get("disappeared_failures", {}),
        "skipped_failures": attribution.get("skipped_failures", {}),
        "exit_code": receipt_exit_code,
    }


def record_backend_pytest(
    *,
    receipt: Path,
    task_id: str,
    directory: str,
    harness: str,
    selectors_json: str,
    attribution_json: str,
    junit: Path,
) -> dict[str, Any]:
    """Merge the targeted backend lane into the sidecar without clobbering it."""

    attribution = json.loads(attribution_json)
    pytest_block = _backend_pytest_block(
        harness=harness,
        selectors=json.loads(selectors_json),
        attribution=attribution,
        junit=junit,
    )
    return merge_receipt(
        receipt,
        lane=LANE_BACKEND_PYTEST,
        block=build_lane_block(
            LANE_BACKEND_PYTEST,
            state=str(attribution["state"]),
            blocking=bool(attribution.get("blocking", False)),
            new_failures=attribution.get("new_failures"),
            suppressed_failures=attribution.get("suppressed_failures"),
            disappeared_failures=attribution.get("disappeared_failures"),
            skipped_failures=attribution.get("skipped_failures"),
            extra={
                "directory": directory,
                "harness": harness,
                "current_exit_code": int(attribution.get("current_exit_code", 0)),
                **(
                    {"error": str(attribution["error"])}
                    if attribution.get("error") is not None
                    else {}
                ),
            },
        ),
        top_level={
            "task_id": task_id,
            "directory": directory,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "ruff": {
                "command": (
                    "python3 -m ruff check --select E,F,W --ignore E501 "
                    "<changed-python-files>"
                ),
                "exit_code": 0,
            },
            "pytest": pytest_block,
        },
    )


def record_backend_pytest_diagnosis(
    *,
    receipt: Path,
    task_id: str,
    directory: str,
    state: str,
    reason: str,
    current_exit_code: int = 1,
) -> bool:
    """Record a blocking backend pre-guard diagnosis without a test report.

    The backend lane can fail before it has a selector roster or JUnit report.
    Keep that diagnosis in the same lane protocol, with all four attribution
    maps present and a blocking marker that can never be mistaken for a pass
    credential.  This is deliberately additive to ``record_backend_pytest``;
    callers with a completed run continue to use the legacy top-level block.
    """

    return record_lane(
        LANE_BACKEND_PYTEST,
        state=state,
        blocking=True,
        extra={
            "task_id": task_id,
            "directory": directory,
            "error": reason,
            "current_exit_code": int(current_exit_code),
        },
        receipt_path=receipt,
    )


# ---------------------------------------------------------------------------
# Multi-lane projection into the PR body and the durable publication receipt
# ---------------------------------------------------------------------------
#
# The durable receipt (backend/app/schemas.py::PublicationGateReceipt) is a
# CLOSED nine-key shape plus one flat attribution block: a single
# ``attribution_state`` string and four flat ``identity -> {detail, reason}``
# maps.  Folding several lanes into that shape needs identities that cannot
# collide across lanes, so every projected identity is prefixed with its lane:
#
#     [static-corpus] backend/testing/test_a.py::test_x::failure
#
# ``backend/app/task_blocked.py`` parses that same prefix back out, so its
# aggregate byte budget can evict fairly across lanes instead of deleting one
# whole lane's suppressions while the receipt still reads clean.

IDENTITY_LANE_PREFIX_RE = re.compile(r"^\[([a-z0-9][a-z0-9._:-]*)\] (?P<identity>.*)$", re.DOTALL)


def namespace_identity(lane: str, identity: str) -> str:
    """Return an identity that carries its lane, readably and reversibly."""

    return f"[{lane}] {identity}"


def split_namespaced_identity(identity: str) -> tuple[str | None, str]:
    """Return ``(lane, bare_identity)``; lane is None for un-namespaced ids."""

    match = IDENTITY_LANE_PREFIX_RE.match(identity)
    if not match:
        return None, identity
    return match.group(1), match.group("identity")


def lane_blocks(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return this sidecar's per-lane blocks, sorted by lane identity.

    Returns an empty list for a legacy sidecar that carries only the top-level
    ``pytest`` block, which is what older and foreign stacks write.
    """

    if not isinstance(payload, Mapping):
        return []
    lanes = payload.get("lanes")
    if not isinstance(lanes, Mapping):
        return []
    blocks: list[dict[str, Any]] = []
    for lane in sorted(str(key) for key in lanes):
        block = lanes[lane]
        if not isinstance(block, Mapping):
            continue
        state = block.get("state")
        if not isinstance(state, str):
            continue
        blocks.append({
            "lane": lane,
            "state": state,
            **{
                key: (block.get(key) if isinstance(block.get(key), Mapping) else {})
                for key in FAILURE_MAP_KEYS
            },
        })
    return blocks


def _legacy_attribution(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the single-lane attribution a pre-multi-lane sidecar carries."""

    if not isinstance(payload, Mapping):
        return None
    pytest_block = payload.get("pytest")
    data = pytest_block if isinstance(pytest_block, Mapping) and "state" in pytest_block else payload
    if not isinstance(data, Mapping) or not isinstance(data.get("state"), str):
        return None
    if not all(isinstance(data.get(key, {}), Mapping) for key in FAILURE_MAP_KEYS):
        return None
    return {
        "attribution_state": data["state"],
        **{key: dict(data.get(key, {})) for key in FAILURE_MAP_KEYS},
    }


def _detail_of(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("detail", ""))
    return str(value)


SUPPRESSED_SECTION_HEADING = "### Suppressed baseline failures"


def render_suppressed_section(payload: Mapping[str, Any] | None) -> str:
    """Return the PR-body section naming every lane's suppressed failures.

    Returns the empty string when nothing was suppressed anywhere -- a clean
    publication has no section to render.  A multi-lane sidecar attributes each
    identity to the lane that forgave it; a legacy sidecar renders exactly the
    flat list it always did, so older and foreign stacks are unchanged.
    """

    blocks = lane_blocks(payload)
    if blocks:
        lines: list[str] = []
        for block in blocks:
            suppressed = block["suppressed_failures"]
            if not suppressed:
                continue
            lines.append(f"- lane `{block['lane']}` (state: {block['state']})")
            for identity in sorted(suppressed):
                lines.append(f"  - `{identity}`: {_detail_of(suppressed[identity])}")
        return "\n".join([SUPPRESSED_SECTION_HEADING, *lines]) if lines else ""
    legacy = _legacy_attribution(payload)
    suppressed = (legacy or {}).get("suppressed_failures") or {}
    if not suppressed:
        return ""
    return "\n".join(
        [SUPPRESSED_SECTION_HEADING]
        + [f"- `{identity}`: {_detail_of(suppressed[identity])}" for identity in sorted(suppressed)]
    )


def project_attribution(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fold every attributed lane into the durable receipt's closed shape.

    ``attribution_state`` names each lane's own state (``lane=state``, joined by
    ``"; "``) so a reader can never mistake one lane's verdict for the whole
    gate's.  Returns ``{}`` for a sidecar that carries no usable attribution --
    the caller then leaves the legacy nine-key receipt untouched.
    """

    blocks = lane_blocks(payload)
    if not blocks:
        return _legacy_attribution(payload) or {}
    projected: dict[str, Any] = {
        "attribution_state": "; ".join(f"{block['lane']}={block['state']}" for block in blocks),
    }
    for key in FAILURE_MAP_KEYS:
        folded: dict[str, Any] = {}
        for block in blocks:
            for identity, value in block[key].items():
                folded[namespace_identity(block["lane"], str(identity))] = value
        projected[key] = folded
    return projected


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def render_suppressed_command(receipt: Path) -> int:
    """Print the suppressed-baseline PR section; never fail the publication."""

    try:
        section = render_suppressed_section(_read_json(receipt))
    except Exception:  # noqa: BLE001 - a receipt never decides a verdict
        return 0
    if section:
        print(section, end="")
    return 0


def project_attribution_command(gate: Path, receipt: Path) -> int:
    """Merge the multi-lane attribution into the durable gate receipt in place.

    A malformed sidecar, an unreadable gate receipt, or an empty projection all
    leave the gate receipt exactly as it was.
    """

    try:
        attribution = project_attribution(_read_json(receipt))
        if not attribution:
            return 0
        gate_payload = _read_json(gate)
        if not isinstance(gate_payload, dict):
            return 0
        gate_payload.update(attribution)
        gate.write_text(
            json.dumps(gate_payload, separators=(",", ":")) + "\n", encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 - a receipt never decides a verdict
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backend = subparsers.add_parser(
        "backend-pytest",
        help="merge the targeted backend pytest lane into the sidecar",
    )
    backend.add_argument("--receipt", type=Path, required=True)
    backend.add_argument("--task-id", required=True)
    backend.add_argument("--directory", required=True)
    backend.add_argument("--harness", required=True)
    backend.add_argument("--selectors-json", required=True)
    # The attribution blob carries one full traceback per suppressed baseline
    # failure, so a busy run can push it past the OS argv-size limit (E2BIG).
    # Callers pass ``--attribution-file`` for that reason; ``--attribution``
    # stays for the small/inline case and for existing callers.
    attribution = backend.add_mutually_exclusive_group(required=True)
    attribution.add_argument("--attribution")
    attribution.add_argument("--attribution-file", type=Path)
    backend.add_argument("--junit", type=Path, required=True)
    diagnosis = subparsers.add_parser(
        "backend-pytest-diagnosis",
        help="record a blocking backend pre-guard diagnosis",
    )
    diagnosis.add_argument("--receipt", type=Path, required=True)
    diagnosis.add_argument("--task-id", required=True)
    diagnosis.add_argument("--directory", required=True)
    diagnosis.add_argument("--state", required=True)
    diagnosis.add_argument("--reason", required=True)
    diagnosis.add_argument("--current-exit-code", type=int, default=1)
    render = subparsers.add_parser(
        "render-suppressed",
        help="print the PR-body section naming every lane's suppressed failures",
    )
    render.add_argument("--receipt", type=Path, required=True)
    project = subparsers.add_parser(
        "project-attribution",
        help="fold every lane's attribution into the durable gate receipt",
    )
    project.add_argument("--gate", type=Path, required=True)
    project.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "render-suppressed":
        return render_suppressed_command(args.receipt)
    if args.command == "project-attribution":
        return project_attribution_command(args.gate, args.receipt)
    if args.command == "backend-pytest-diagnosis":
        record_backend_pytest_diagnosis(
            receipt=args.receipt,
            task_id=args.task_id,
            directory=args.directory,
            state=args.state,
            reason=args.reason,
            current_exit_code=args.current_exit_code,
        )
        return 0

    attribution_json = (
        args.attribution
        if args.attribution is not None
        else args.attribution_file.read_text(encoding="utf-8")
    )
    record_backend_pytest(
        receipt=args.receipt,
        task_id=args.task_id,
        directory=args.directory,
        harness=args.harness,
        selectors_json=args.selectors_json,
        attribution_json=attribution_json,
        junit=args.junit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
