"""Operator-approval justifications on cap-bump-history ledger records.

Raising a CHRONIC size-cap offender needs an operator's approval:
``check_size_limits.py --fix --force-chronic`` refuses unless
``SIZE_CAP_OPERATOR_APPROVAL`` is set.  Until 2026-08-26 that was a PRESENCE
gate only — the value was never read and never stored — and the ledger record
schema was exactly ``{commit_sha, commit_date, old_cap, new_cap}``.  So a
chronic bump left no durable record of who authorised it or why: the only
surviving audit surface was a commit message nothing validates, nothing links
to the record, and a squash or rewrite can lose.  A reviewer reading
``cap_bump_history.json`` six months later could not tell an operator's
headroom decision from a bump someone forced to go green.

This module owns the durable half of that fix — stamping the approval text
onto the ledger record as ``justification`` and, just as importantly, keeping
it there.  ``rebuild_cap_bump_history_ledger`` reconstructs records from
``git log -p`` on a complete checkout, and that reconstruction knows only the
four cap fields, so without an explicit carry-forward every recorded approval
would be dropped by the very next ``--fix``.

It is a sibling of ``size_limit_policy`` rather than more code inside it
because that module sits at its own line-count cap; the split also keeps the
justification rules readable on their own and importable without the
git-shelling ledger machinery.

Stdlib-only and dependency-free, matching ``size_limit_policy``'s contract so
the standalone checker can reach it with no install step.
"""

from __future__ import annotations

# Ledger record key holding the operator approval text for a chronic cap bump.
# The field is OPTIONAL by design: hundreds of records predate it (184 written
# on 2026-08-26 alone) and are never backfilled with a fabricated reason —
# absent means absent, and every reader must tolerate it.
LEDGER_JUSTIFICATION_FIELD = "justification"

# A record is identified by (section, module_key, old_cap, new_cap) — exactly
# the tuple ``check_size_limits._record_justifies`` matches the ratchet on, so
# nothing in this module can change which raise a record justifies.


def justification_index(history: dict | None) -> dict:
    """Map ``(section, key, old_cap, new_cap)`` -> justification text.

    Reads a ledger's ``history`` mapping (section -> module key -> records)
    defensively: anything malformed is skipped rather than raised on, because
    this index only ever ADDS provenance to a record that has none.  A blank or
    non-string justification is treated as absent.
    """
    index: dict = {}
    for section, keys in (history or {}).items():
        if not isinstance(keys, dict):
            continue
        for key, records in keys.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                text = record.get(LEDGER_JUSTIFICATION_FIELD)
                if isinstance(text, str) and text.strip():
                    index[
                        (section, key, record.get("old_cap"), record.get("new_cap"))
                    ] = text.strip()
    return index


def apply_ledger_justifications(
    ledger: dict,
    prior_history: dict | None = None,
    justifications: dict | None = None,
) -> dict:
    """Stamp and preserve operator justifications on ``ledger``'s records.

    Two jobs, both keyed on the record identity described above:

    * ``justifications`` — ``{(section, key, old_cap, new_cap): text}`` for
      bumps this run just approved — stamps the approval onto its record.
    * ``prior_history`` — the ledger's ``history`` mapping as it was read from
      disk BEFORE any git regeneration — carries an already-recorded
      justification forward onto the regenerated record for the same identity.

    An existing justification is never overwritten (the first recorded
    authorisation is the one that happened) and a missing one is never
    invented, so legacy records keep exactly the four keys they were written
    with.  Records are mutated in place and ``ledger`` is returned for
    chaining.
    """
    result = dict(ledger) if isinstance(ledger, dict) else {}
    history = result.get("history")
    if not isinstance(history, dict):
        return result
    resolved = justification_index(prior_history)
    for identity, text in (justifications or {}).items():
        text = str(text).strip()
        if text:
            resolved[identity] = text
    if not resolved:
        return result
    for section, keys in history.items():
        if not isinstance(keys, dict):
            continue
        for key, records in keys.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                if record.get(LEDGER_JUSTIFICATION_FIELD):
                    continue
                text = resolved.get(
                    (section, key, record.get("old_cap"), record.get("new_cap"))
                )
                if text:
                    record[LEDGER_JUSTIFICATION_FIELD] = text
    return result
