#!/usr/bin/env python3
"""Shared, bounded suppression-receipt emitter for every ``./check`` guard lane.

Only the targeted backend pytest lane used to leave a machine-readable record of
what it suppressed; the static, vitest, typecheck and viewport lanes printed
their suppressions to stdout where nothing consumed them.  This module is the
producer half of a single answer to "what did the guards suppress on this
candidate?": every lane writes one bounded JSON file into a task-scoped
directory, and :func:`aggregate` merges them into one ``{lanes, totals}`` object.

Contract, deliberately narrow:

* **Emitting never changes a verdict.**  :func:`record_lane` is fail-open — any
  write error warns on stderr and returns ``None``.  Callers ignore the return
  value; no lane's exit code, blocking semantics or suppression policy depends
  on this module.
* **Payloads are bounded.**  A single suppressed pytest failure can carry a full
  traceback, so both the entry count per bucket and the length of every recorded
  string are capped.  The *observed* (pre-truncation) count is recorded next to
  the truncated entries so a reader can always tell truncation happened.
* **Identities are machine-readable.**  Bucket keys are the guards' route/test
  identity keys, never prose.

Bash callers invoke the argv CLI (``python3 scripts/guard_suppression_receipt.py
record --lane ... --state ...``) rather than embedding Python source, per the
generated-shell rules in CLAUDE.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: Maximum number of entries recorded per bucket (suppressed/disappeared/skipped).
MAX_ENTRIES_PER_BUCKET = 25

#: Maximum length of any single recorded string (keys, details, reasons).
MAX_STRING_LENGTH = 512

#: Env var that pins the receipt directory.  ``./check`` exports it so the
#: generated script's task-id fallback and ``check-default.sh``'s
#: ``local-developer`` fallback both reach the same directory the entry point
#: cleaned at startup.
RECEIPT_DIR_ENV = "BLUEPRINT_GUARD_SUPPRESSION_DIR"

#: Same convention ``scripts/check-default.sh`` uses for its verification receipt.
DEFAULT_TASK_ID = "local-developer"

_BUCKETS = ("suppressed", "disappeared", "skipped")

_LANE_SAFE = re.compile(r"[^A-Za-z0-9._:-]+")


def resolve_task_id(task_id: str | None = None) -> str:
    """Return the task identity receipts are scoped to."""

    candidate = (task_id or os.environ.get("TASK_ID", "")).strip()
    return candidate or DEFAULT_TASK_ID


def receipt_dir(task_id: str | None = None) -> Path:
    """Return the task-scoped directory holding one JSON file per lane."""

    pinned = os.environ.get(RECEIPT_DIR_ENV, "").strip()
    if pinned:
        return Path(pinned)
    return Path("/tmp") / f"guard_suppression_receipts_{resolve_task_id(task_id)}"


def _lane_filename(lane: str) -> str:
    return f"lane_{_LANE_SAFE.sub('_', lane.strip()) or 'unknown'}.json"


def lane_receipt_path(lane: str, task_id: str | None = None) -> Path:
    """Return the per-lane receipt path for ``lane``."""

    return receipt_dir(task_id) / _lane_filename(lane)


def _clip(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    if len(text) > MAX_STRING_LENGTH:
        return text[: MAX_STRING_LENGTH - 1] + "…"
    return text


def _bucket(entries: Mapping[str, Any] | Iterable[str] | None) -> dict[str, Any]:
    """Normalize one bucket into bounded ``{entries, observed_count, truncated}``.

    Accepts either the guards' ``key -> {detail, reason}`` mapping, a plain
    ``key -> detail`` mapping, or a bare sequence of identity keys.
    """

    if entries is None:
        items: list[tuple[str, Any]] = []
    elif isinstance(entries, Mapping):
        items = sorted(((str(key), value) for key, value in entries.items()), key=lambda kv: kv[0])
    else:
        items = [(str(key), "") for key in sorted(str(key) for key in entries)]

    observed = len(items)
    kept = items[:MAX_ENTRIES_PER_BUCKET]
    recorded: dict[str, dict[str, str]] = {}
    for key, value in kept:
        if isinstance(value, Mapping):
            record = {
                str(field): _clip(field_value)
                for field, field_value in sorted(value.items())
                if field in ("detail", "reason")
            }
        else:
            record = {"detail": _clip(value)}
        recorded[_clip(key)] = record
    return {
        "entries": recorded,
        "observed_count": observed,
        "recorded_count": len(recorded),
        "truncated": observed > len(recorded),
    }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def build_lane_record(
    lane: str,
    *,
    state: str,
    suppressed: Mapping[str, Any] | Iterable[str] | None = None,
    disappeared: Mapping[str, Any] | Iterable[str] | None = None,
    skipped: Mapping[str, Any] | Iterable[str] | None = None,
    note: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Return the bounded JSON-serializable record for one lane."""

    record: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "lane": _clip(lane),
        "task_id": resolve_task_id(task_id),
        "state": _clip(state),
        "recorded_at": _now(),
        "buckets": {
            "suppressed": _bucket(suppressed),
            "disappeared": _bucket(disappeared),
            "skipped": _bucket(skipped),
        },
    }
    if note:
        record["note"] = _clip(note)
    return record


def record_lane(
    lane: str,
    *,
    state: str,
    suppressed: Mapping[str, Any] | Iterable[str] | None = None,
    disappeared: Mapping[str, Any] | Iterable[str] | None = None,
    skipped: Mapping[str, Any] | Iterable[str] | None = None,
    note: str | None = None,
    task_id: str | None = None,
) -> Path | None:
    """Write one lane's suppression receipt.  Fail-open: never raises.

    Returns the written path, or ``None`` when the receipt could not be written
    (the caller's verdict and exit code are unaffected either way).
    """

    try:
        record = build_lane_record(
            lane,
            state=state,
            suppressed=suppressed,
            disappeared=disappeared,
            skipped=skipped,
            note=note,
            task_id=task_id,
        )
        destination = lane_receipt_path(lane, task_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(destination)
        return destination
    except Exception as exc:  # noqa: BLE001 - emitting must never block a lane
        print(
            f"[guard-suppression] WARNING: could not record {lane} suppression receipt: {exc}",
            file=sys.stderr,
        )
        return None


def record_classification(
    lane: str,
    outcome: Mapping[str, Any],
    *,
    note: str | None = None,
    task_id: str | None = None,
) -> Path | None:
    """Record a lane from a ``classify_verification`` outcome dict.

    This is the call site every differential guard lane uses, so the four lanes
    that only printed their suppressions now land in the same aggregate as the
    backend pytest lane.
    """

    return record_lane(
        lane,
        state=str(outcome.get("state", "unknown")),
        suppressed=outcome.get("suppressed_failures"),
        disappeared=outcome.get("disappeared_failures"),
        skipped=outcome.get("skipped_failures"),
        note=note,
        task_id=task_id,
    )


def aggregate(task_id: str | None = None) -> dict[str, Any]:
    """Merge every per-lane receipt into one ``{lanes, totals}`` object."""

    directory = receipt_dir(task_id)
    lanes: dict[str, Any] = {}
    unreadable: list[str] = []
    try:
        candidates = sorted(directory.glob("lane_*.json"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            unreadable.append(path.name)
            continue
        lane = str(record.get("lane") or path.stem)
        lanes[lane] = record

    totals = {bucket: 0 for bucket in _BUCKETS}
    totals_observed = {f"{bucket}_observed": 0 for bucket in _BUCKETS}
    truncated = False
    lanes_with_suppressions: list[str] = []
    for lane, record in lanes.items():
        buckets = record.get("buckets") or {}
        for bucket in _BUCKETS:
            payload = buckets.get(bucket) or {}
            totals[bucket] += int(payload.get("recorded_count") or 0)
            totals_observed[f"{bucket}_observed"] += int(payload.get("observed_count") or 0)
            truncated = truncated or bool(payload.get("truncated"))
        if (buckets.get("suppressed") or {}).get("observed_count"):
            lanes_with_suppressions.append(lane)

    return {
        "version": SCHEMA_VERSION,
        "task_id": resolve_task_id(task_id),
        "directory": str(directory),
        "lanes": lanes,
        "totals": {
            "lanes": len(lanes),
            **totals,
            **totals_observed,
            "truncated": truncated,
            "lanes_with_suppressions": sorted(lanes_with_suppressions),
            "unreadable_lane_files": sorted(unreadable),
        },
    }


def project_for_gate_receipt(aggregate_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project an aggregate into the durable gate-receipt shape.

    The PR body renders the aggregate directly, but the durable receipt on
    ``task.context`` is a flatter projection.  Two properties are load-bearing
    and are why this lives here (Python, unit-tested) rather than in an inline
    ``python3 -c`` one-liner inside the generated ./pr script:

    * **All three buckets survive.**  Some lanes record their only identity in
      ``disappeared`` or ``skipped`` (the viewport lane's capture-skipped branch
      records nothing else), so a suppressed-only projection would drop the very
      content the receipt exists to make reviewable.
    * **Truncation stays visible.**  Each bucket's producer-side
      ``observed_count``/``truncated`` are carried alongside the (already
      capped) entries, so a reader can always tell that ``MAX_ENTRIES_PER_BUCKET``
      clipped the set instead of seeing a complete-looking list.
    """

    lanes = aggregate_payload.get("lanes") if isinstance(aggregate_payload, Mapping) else None
    if not isinstance(lanes, Mapping):
        return {}
    projection: dict[str, Any] = {}
    for lane, record in lanes.items():
        if not isinstance(record, Mapping):
            continue
        state = record.get("state")
        lane_record: dict[str, Any] = {"state": state if isinstance(state, str) else "unknown"}
        buckets = record.get("buckets")
        buckets = buckets if isinstance(buckets, Mapping) else {}
        for bucket in _BUCKETS:
            payload = buckets.get(bucket)
            payload = payload if isinstance(payload, Mapping) else {}
            raw_entries = payload.get("entries")
            raw_entries = raw_entries if isinstance(raw_entries, Mapping) else {}
            entries = {
                str(key): (
                    str(value.get("detail", "")) if isinstance(value, Mapping) else str(value)
                )
                for key, value in raw_entries.items()
            }
            observed = payload.get("observed_count")
            observed = int(observed) if isinstance(observed, int) and observed >= len(entries) else len(entries)
            if bucket != "suppressed" and not entries and not observed:
                # Keep the durable payload small: an empty non-suppressed bucket
                # carries no audit signal.  ``suppressed`` is always emitted so
                # the shape stays stable for existing consumers.
                continue
            lane_record[bucket] = entries
            lane_record[f"{bucket}_observed_count"] = observed
            lane_record[f"{bucket}_truncated"] = bool(payload.get("truncated")) or observed > len(entries)
        projection[str(lane)] = lane_record
    return projection


def merge_into_gate_receipt(aggregate_path: Path, gate_receipt_path: Path) -> bool:
    """Write ``guard_suppressions`` into the gate receipt.  Fail-open.

    Returns ``True`` when the receipt was rewritten.  Any read/parse/write
    problem warns on stderr and returns ``False`` — the publication verdict must
    never depend on this projection.
    """

    try:
        aggregate_payload = json.loads(Path(aggregate_path).read_text(encoding="utf-8"))
        gate = json.loads(Path(gate_receipt_path).read_text(encoding="utf-8"))
        if not isinstance(gate, dict):
            return False
        projection = project_for_gate_receipt(aggregate_payload)
        if not projection:
            return False
        gate["guard_suppressions"] = projection
        Path(gate_receipt_path).write_text(
            json.dumps(gate, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        return True
    except Exception as exc:  # noqa: BLE001 - projection must never block ./pr
        print(
            f"[guard-suppression] WARNING: could not project aggregate into gate receipt: {exc}",
            file=sys.stderr,
        )
        return False


def _parse_identity(raw: str) -> tuple[str, str]:
    key, separator, detail = raw.partition("=")
    return (key if separator else raw), (detail if separator else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="record one lane's suppression receipt")
    record.add_argument("--lane", required=True)
    record.add_argument("--state", required=True)
    record.add_argument("--note")
    record.add_argument("--task-id")
    for bucket in _BUCKETS:
        record.add_argument(
            f"--{bucket}",
            action="append",
            default=[],
            metavar="KEY[=DETAIL]",
            help=f"machine-readable identity for the {bucket} bucket (repeatable)",
        )

    show = sub.add_parser("aggregate", help="merge every lane receipt into one object")
    show.add_argument("--task-id")
    show.add_argument("--out", type=Path, help="write the aggregate here instead of stdout")

    project = sub.add_parser(
        "project", help="merge the aggregate into a gate receipt under guard_suppressions"
    )
    project.add_argument("--aggregate", required=True, type=Path)
    project.add_argument("--gate-receipt", required=True, type=Path)

    where = sub.add_parser("dir", help="print the task-scoped receipt directory")
    where.add_argument("--task-id")

    args = parser.parse_args(argv)

    if args.command == "record":
        buckets = {
            bucket: dict(_parse_identity(raw) for raw in getattr(args, bucket))
            for bucket in _BUCKETS
        }
        path = record_lane(
            args.lane,
            state=args.state,
            note=args.note,
            task_id=args.task_id,
            **buckets,
        )
        if path is not None:
            print(f"[guard-suppression] {args.lane} receipt: {path}")
        # Fail-open: a write failure must not change the caller's exit code.
        return 0

    if args.command == "aggregate":
        payload = json.dumps(aggregate(args.task_id), sort_keys=True)
        if args.out is not None:
            try:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(payload + "\n", encoding="utf-8")
            except OSError as exc:
                print(
                    f"[guard-suppression] WARNING: could not write aggregate: {exc}",
                    file=sys.stderr,
                )
                return 0
            print(f"[guard-suppression] aggregate: {args.out}")
            return 0
        print(payload)
        return 0

    if args.command == "project":
        merge_into_gate_receipt(args.aggregate, args.gate_receipt)
        # Fail-open: a missing/malformed aggregate must not change ./pr's verdict.
        return 0

    print(receipt_dir(args.task_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
