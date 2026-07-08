"""3-way vision merge with user-bias and rejected-edit logging.

Audit 2026-04-25 W2/D7: the previous merge in ``strategic_completion``
only protected ``purpose`` and ``success_criteria``; ``systems``,
``priorities``, and ``context`` were always overwritten by the agent
even when the user had edited them mid-cycle. The user's call
(2026-04-26): "make the strategic agent favor the user, but probably
keep both" — i.e. user wins on the persisted vision, but the agent's
rejected proposal is captured so next cycle's agent can see what was
overridden and reconcile.

Three inputs:
  - ``baseline`` — vision as the agent saw it at cycle start (snapshot
    persisted in ``StrategicCycle.cycle_metadata['baseline_vision']``).
  - ``agent`` — vision returned by the agent at cycle end.
  - ``current`` — vision in the DB right now (may include user edits
    made during the cycle).

Conflict resolution per top-level field:
  - If ``current[k] == baseline[k]`` (user didn't touch it):
    take ``agent[k]`` if present.
  - If ``current[k] != baseline[k]`` (user edited it) AND
    ``agent[k] != baseline[k]`` (agent also tried to edit it):
    keep ``current[k]`` (user wins) AND log the agent's value to
    the rejected-edits ledger so next cycle sees the bypass.
  - If only the user changed it: keep ``current[k]``.
  - If only the agent changed it: take ``agent[k]``.

Lists (``systems``, ``priorities``, ``success_criteria`` items):
  - Match by ``id`` when present, else by ``name``/``label``/``text``.
  - Per-item: same field-level rule as above.
  - Reorders are not detected (treated as no-op); item adds/removes
    follow the rule (user removal beats agent re-add, agent add lands).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from app.time_utils import _utcnow


REJECTED_EDITS_KEY = "_rejected_agent_edits"
# Cap so the ledger doesn't bloat vision; older entries roll off.
REJECTED_EDITS_MAX = 30

# Top-level vision fields the agent is allowed to author (the merge
# operates on these; the agent can add new keys outside this set, those
# fall through as agent-wins by default).
_KNOWN_AGENT_FIELDS = (
    "purpose",
    "success_criteria",
    "systems",
    "priorities",
    "context",
)


def _item_key(item: Any) -> Optional[str]:
    """Identify a list item for cross-version matching. Returns ``id``,
    falling back to ``name`` / ``label`` / ``text`` when the list shape
    doesn't carry an id (e.g. success_criteria list of strings)."""
    if not isinstance(item, dict):
        return str(item) if item is not None else None
    for key in ("id", "name", "label", "text", "title"):
        v = item.get(key)
        if v:
            return f"{key}:{v}"
    return None


def _merge_list(
    baseline: list,
    agent: list,
    current: list,
    *,
    field_path: str,
    rejected: list,
) -> list:
    """Per-item field-level merge for a list of dicts (or strings).

    Items present in ``current`` always survive (user added/kept). Items
    only in ``agent`` are added (agent extension). Items the user removed
    are NOT re-added (user removal wins); the agent's would-be re-add is
    logged to ``rejected``.
    """
    base_by_key = {k: v for v in (baseline or []) if (k := _item_key(v)) is not None}
    agent_by_key = {k: v for v in (agent or []) if (k := _item_key(v)) is not None}

    merged: list = []
    seen: set[str] = set()
    # Walk current order to preserve user's ordering / additions.
    for item in current or []:
        key = _item_key(item)
        if key is None:
            merged.append(item)
            continue
        if key in seen:
            continue
        seen.add(key)
        agent_item = agent_by_key.get(key)
        baseline_item = base_by_key.get(key)
        if agent_item is None:
            # User has it; agent dropped or never had it. Keep user's.
            merged.append(item)
            if key in base_by_key:
                # Agent removed an item the baseline had — log the rejection.
                rejected.append(
                    {
                        "at": _utcnow().isoformat(),
                        "field": f"{field_path}[{key}]",
                        "kind": "removal_rejected",
                        "user_value": item,
                        "agent_value": None,
                    }
                )
            continue
        # Both have it. Field-level merge if dict; else user wins.
        if isinstance(item, dict) and isinstance(agent_item, dict):
            merged_item = _merge_dict(
                baseline_item if isinstance(baseline_item, dict) else {},
                agent_item,
                item,
                field_path=f"{field_path}[{key}]",
                rejected=rejected,
            )
            merged.append(merged_item)
        else:
            # Scalar list items: user wins on conflict.
            if item != agent_item and item != baseline_item:
                rejected.append(
                    {
                        "at": _utcnow().isoformat(),
                        "field": f"{field_path}[{key}]",
                        "kind": "value_conflict",
                        "user_value": item,
                        "agent_value": agent_item,
                    }
                )
            merged.append(item)
    # Items only in agent (additions).
    for key, agent_item in agent_by_key.items():
        if key in seen:
            continue
        if key in base_by_key:
            # Existed in baseline, current dropped it — user removed it. Don't re-add.
            rejected.append(
                {
                    "at": _utcnow().isoformat(),
                    "field": f"{field_path}[{key}]",
                    "kind": "user_removed_agent_kept",
                    "user_value": None,
                    "agent_value": agent_item,
                }
            )
            continue
        # Brand new item from agent: user hasn't seen it, take it.
        merged.append(agent_item)
        seen.add(key)
    return merged


def _merge_dict(
    baseline: Mapping[str, Any],
    agent: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    field_path: str,
    rejected: list,
) -> dict:
    merged: dict = dict(current)
    all_keys = set(baseline) | set(agent) | set(current)
    for k in all_keys:
        b = baseline.get(k)
        a = agent.get(k)
        c = current.get(k)
        if k not in agent:
            # Agent didn't author this key — leave current as-is.
            continue
        if c == b:
            # User didn't touch this key — take agent's value.
            merged[k] = a
            continue
        if a == b:
            # Agent didn't actually change it — keep user's.
            continue
        # Both user and agent changed it. Recursively merge if dict/list,
        # else keep user's value and log the rejection.
        if isinstance(a, dict) and isinstance(c, dict):
            merged[k] = _merge_dict(
                b if isinstance(b, dict) else {},
                a,
                c,
                field_path=f"{field_path}.{k}",
                rejected=rejected,
            )
        elif isinstance(a, list) and isinstance(c, list):
            merged[k] = _merge_list(
                b if isinstance(b, list) else [],
                a,
                c,
                field_path=f"{field_path}.{k}",
                rejected=rejected,
            )
        else:
            # Scalar conflict: user wins, log agent's intent.
            rejected.append(
                {
                    "at": _utcnow().isoformat(),
                    "field": f"{field_path}.{k}",
                    "kind": "value_conflict",
                    "user_value": c,
                    "agent_value": a,
                }
            )
            # merged already has c (user value); leave it.
    return merged


def merge_vision(
    baseline: Mapping[str, Any],
    agent: Mapping[str, Any],
    current: Mapping[str, Any],
) -> tuple[dict, list]:
    """Three-way merge.

    Returns ``(merged_vision, rejected_edits)``. The caller is responsible
    for appending ``rejected_edits`` to ``current[REJECTED_EDITS_KEY]``
    (capped at ``REJECTED_EDITS_MAX``) before persisting.
    """
    rejected: list = []
    merged = _merge_dict(
        baseline or {},
        agent or {},
        current or {},
        field_path="vision",
        rejected=rejected,
    )
    return merged, rejected


def _rejected_edit_fingerprint(entry: Any) -> tuple:
    """Stable content key for dedup: (field, kind, repr(agent_value)).

    Two entries with the same field/kind/agent_value are replay duplicates;
    the ``at`` timestamp differs on retry but the logical content is the same.
    Dedup is intentionally content-only (ignores ``at``). Entries missing
    field/kind/agent_value are not expected in production; they all share the
    (None, None, 'None') fingerprint and collapse to one.
    """
    return (entry.get("field"), entry.get("kind"), repr(entry.get("agent_value")))


def append_rejected_edits(vision: dict, new_rejected: Iterable[dict]) -> dict:
    """Append rejected-edit entries to ``vision[REJECTED_EDITS_KEY]`` and
    cap the list at ``REJECTED_EDITS_MAX``. Returns the mutated vision dict
    so the caller can ``flag_modified`` once.

    Entries that duplicate an existing (field, kind, agent_value) fingerprint
    are dropped so a reconciliation replay cannot inflate the ledger.
    """
    new_list = list(new_rejected or [])
    if not new_list:
        return vision
    existing = list(vision.get(REJECTED_EDITS_KEY) or [])
    seen = {_rejected_edit_fingerprint(e) for e in existing}
    for e in new_list:
        fp = _rejected_edit_fingerprint(e)
        if fp not in seen:
            existing.append(e)
            seen.add(fp)
    vision[REJECTED_EDITS_KEY] = existing[-REJECTED_EDITS_MAX:]
    return vision
