"""Durable serialization and coverage proof for the review issue ledger."""

from __future__ import annotations

import hashlib
import json
from typing import Any



ISSUE_LEDGER_MAX_RESOLVED = 30
ISSUE_LEDGER_SCHEMA_VERSION = 1


def _history_entry_key(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    # Freeform feedback is intentionally excluded: persistence may truncate it,
    # while every authority-bearing structured field remains byte-stable.
    structured = {key: value for key, value in entry.items() if key != "feedback"}
    encoded = json.dumps(structured, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _history_entry_task_id(entry: object) -> str:
    """Stable identity of a review-history row: the review task that wrote it.

    ``_history_entry_key`` hashes the row's whole structured body, which makes
    it a *content* fingerprint, not an identity.  review_history is a rewritable
    list -- ``review_request_changes`` rebuilds the parent history by filtering
    rows out by ``review_task_id`` and re-appending a recomputed entry, and any
    write path may enrich a row after the ledger was stamped against it.  A
    single such rewrite changes the hash, the durable lineage marker stops
    matching anything, and (before the recovery path in ``build_issue_ledger``)
    the ledger froze permanently.

    ``review_task_id`` is assigned by the backend when the row is created and is
    never rewritten, so it survives exactly the rewrites the content hash does
    not.  It is stamped alongside the hash and consulted first; the hash remains
    the fallback for rows and legacy seeds that predate this field.
    """
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("review_task_id") or "").strip()


def normalize_issue_ledger(value: object) -> dict[str, Any] | None:
    """Return a safe copy of a durable ledger, or ``None`` when malformed."""
    if not isinstance(value, dict):
        return None
    raw_open = value.get("open")
    raw_resolved = value.get("resolved")
    if not isinstance(raw_open, list) or not isinstance(raw_resolved, list):
        return None
    open_rows: list[dict] = []
    seen_ids: set[str] = set()
    for raw in raw_open:
        if not isinstance(raw, dict):
            return None
        row = dict(raw)
        issue_id = str(row.get("issue_id") or "").strip()
        severity = str(row.get("severity") or "").upper()
        description = str(row.get("description") or "").strip()
        if (
            not issue_id
            or issue_id in seen_ids
            or severity not in ("CRITICAL", "MAJOR")
            or not description
        ):
            return None
        seen_ids.add(issue_id)
        row["issue_id"] = issue_id
        row["severity"] = severity.lower()
        row["description"] = description
        open_rows.append(row)
    resolved_rows = [dict(row) for row in raw_resolved if isinstance(row, dict)]
    if len(resolved_rows) != len(raw_resolved):
        return None
    normalized = {
        "open": open_rows,
        "resolved": resolved_rows[-ISSUE_LEDGER_MAX_RESOLVED:],
    }
    if (
        value.get("schema_version") == ISSUE_LEDGER_SCHEMA_VERSION
        and isinstance(value.get("complete"), bool)
        and str(value.get("through_entry_key") or "")
    ):
        normalized.update(
            {
                "schema_version": ISSUE_LEDGER_SCHEMA_VERSION,
                "complete": value["complete"],
                "through_entry_key": str(value["through_entry_key"]),
            }
        )
        # Additive: the stable anchor rides alongside the legacy content hash
        # so a seed written by this version keeps matching after a row rewrite,
        # while a seed written before it still normalizes cleanly without one.
        through_task_id = str(value.get("through_entry_task_id") or "").strip()
        if through_task_id:
            normalized["through_entry_task_id"] = through_task_id
    return normalized


def issue_ledger_complete_for_history(
    ledger: object, review_history: list | None
) -> bool:
    """Return whether a backend ledger proves coverage through retained tail."""
    normalized = normalize_issue_ledger(ledger)
    tail = next(
        (entry for entry in reversed(review_history or []) if isinstance(entry, dict)),
        None,
    )
    if not (
        normalized
        and normalized.get("schema_version") == ISSUE_LEDGER_SCHEMA_VERSION
        and normalized.get("complete") is True
        and tail is not None
    ):
        return False
    # Either anchor proves the ledger covers this tail.  The stable anchor is
    # checked first because it survives a post-stamp rewrite of the row; the
    # content hash still answers for seeds stamped before the anchor existed.
    seed_task_id = str(normalized.get("through_entry_task_id") or "").strip()
    tail_task_id = _history_entry_task_id(tail)
    if seed_task_id and tail_task_id:
        return seed_task_id == tail_task_id
    return normalized.get("through_entry_key") == _history_entry_key(tail)


def _seed_pending_disposition(row: dict) -> dict | None:
    """Round-trip the non-authoritative ``pending_disposition`` annotation.

    Additive bookkeeping only (see ``build_issue_ledger``'s resolution loop in
    ``app.issue_ledger``): it never gates open/resolved membership, so a
    malformed value is simply dropped rather than failing the whole seed.
    """
    raw = row.get("pending_disposition")
    if not isinstance(raw, dict):
        return None
    disposition = str(raw.get("disposition") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    if not disposition or not reason:
        return None
    return {
        "disposition": disposition,
        "reason": reason,
        "iteration": int(raw.get("iteration") or 0),
        "head_sha": str(raw.get("head_sha") or ""),
        "review_task_id": (
            str(raw["review_task_id"]) if raw.get("review_task_id") else None
        ),
    }


def _seed_open_record(row: dict) -> dict:
    issue = {
        "issue_id": row["issue_id"],
        "severity": str(row["severity"]).upper(),
        "description": row["description"],
        **({"file": row.get("file")} if row.get("file") else {}),
        **({"line": row.get("line")} if row.get("line") is not None else {}),
        **({"symbol": row.get("symbol")} if row.get("symbol") else {}),
    }
    pending_disposition = _seed_pending_disposition(row)
    return {
        "issue_id": row["issue_id"],
        "issue": issue,
        "demanded_iteration": int(row.get("demanded_iteration") or 0),
        "demanded_head_sha": str(row.get("demanded_head_sha") or ""),
        "demanded_base_sha": str(row.get("demanded_base_sha") or ""),
        "demanded_target_verified": bool(row.get("demanded_target_verified")),
        "last_seen_iteration": int(row.get("last_seen_iteration") or 0),
        "last_seen_head_sha": str(row.get("last_seen_head_sha") or ""),
        "last_seen_base_sha": str(row.get("last_seen_base_sha") or ""),
        "last_seen_target_verified": bool(row.get("last_seen_target_verified")),
        **(
            {"pending_disposition": pending_disposition}
            if pending_disposition is not None
            else {}
        ),
    }
