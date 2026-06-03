"""Operator-wins reconciliation for ``project.running_update``.

``running_update`` is a single living markdown document the strategic agent
rewrites each cycle. It has two agent write paths that must not drift apart:

  - the LIVE ``set_running_update`` strategic op
    (``routers/admin_agent_ops.py``), used every cycle, and
  - the FALLBACK log-parse path in ``strategic_completion`` that only runs
    when the agent did NOT use the live API.

Both previously blind-assigned ``project.running_update = <agent doc>`` with
no baseline check, so an operator edit (or a system routine-event digest line)
made while a cycle was running was silently clobbered by the agent's refreshed
write. This module is the single shared reconciliation both paths call so they
share one implementation.

The reconciliation mirrors the operator-wins treatment vision and
problem_model already get (see ``vision_merge`` / the ``_rejected_agent_edits``
ledger):

  - ``current == baseline`` (nothing changed it since the agent got the
    baseline): take the agent's document.
  - ``current`` differs from ``baseline`` ONLY by appended routine-event digest
    lines: this is NOT an operator edit (digests are appended independent of
    cycle timing by ``Project.append_routine_digest`` /
    ``admin_notifications.append_routine_digest``). Take the agent's rewrite and
    re-integrate the mid-cycle digest lines by appending them to the agent's
    fresh document, so a routine event that fired during the cycle is not lost.
  - otherwise (the operator genuinely edited the body): keep the current
    document (operator wins) and record the agent's superseded document into
    the existing rejected-edits ledger so it surfaces to next cycle's agent.
  - ``baseline is None`` (no cycle/baseline resolvable — legacy task): degrade
    to the historical blind-assign behavior, taking the agent's document.
"""

from __future__ import annotations

import re

from sqlalchemy.orm.attributes import flag_modified

from app.models import Project
from app.time_utils import _utcnow
from app.vision_merge import append_rejected_edits


# A routine-event digest line has the exact one-line shape produced by
# ``Project.append_routine_digest``:
#     ``- [YYYY-MM-DD HH:MM] **<event_type>**: <summary>``
# The structural shape is matched here; ``event_type`` membership in
# ``ROUTINE_EVENT_TYPES`` is validated separately so an operator line that
# merely resembles the shape is not mistaken for a digest.
_DIGEST_LINE_RE = re.compile(
    r"^- \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] \*\*(?P<event_type>[a-z0-9_]+)\*\*: "
)

# Marker stored on the rejected-edit ledger entry so a superseded
# running_update document is distinguishable from the vision/problem-model
# field conflicts that share the ledger.
RUNNING_UPDATE_REJECTED_KIND = "running_update_superseded"


def _routine_event_types() -> frozenset:
    """Lazily import the routine-event allowlist.

    Imported at call time so this pure helper module has no import-time
    dependency on the (heavier) notifications router and cannot create a
    circular import.
    """
    from app.routers.admin_notifications import ROUTINE_EVENT_TYPES

    return ROUTINE_EVENT_TYPES


def _detect_digest_append(baseline: str, current: str) -> list[str] | None:
    """Classify how ``current`` relates to ``baseline``.

    Returns:
      - ``None`` when ``current`` is anything other than ``baseline`` plus
        appended routine-digest lines (i.e. an operator edited the body).
      - ``[]`` when ``current`` equals ``baseline`` (modulo trailing
        whitespace ``append_routine_digest`` would have stripped).
      - ``[line, ...]`` the appended digest lines when ``current`` is exactly
        ``baseline`` followed by one or more routine-event digest lines.

    ``append_routine_digest`` rstrips the existing document before appending
    ``"\\n" + line``, so the comparison is anchored at ``baseline.rstrip()``.
    """
    base_r = baseline.rstrip()
    if current in (baseline, base_r):
        return []
    if base_r == "":
        remainder = current
    elif current.startswith(base_r + "\n"):
        remainder = current[len(base_r) + 1:]
    else:
        return None
    if remainder == "":
        return []
    lines = remainder.split("\n")
    routine_types = _routine_event_types()
    for line in lines:
        m = _DIGEST_LINE_RE.match(line)
        if not m or m.group("event_type") not in routine_types:
            return None
    return lines


def _reintegrate_digest(agent: str, digest_lines: list[str]) -> str:
    """Append ``digest_lines`` onto the agent's fresh document, matching the
    ``append_routine_digest`` join shape (rstrip then ``"\\n" + line``)."""
    out = agent.rstrip()
    for line in digest_lines:
        out = f"{out}\n{line}" if out else line
    return out


def reconcile_running_update(
    baseline: str | None,
    agent: str,
    current: str | None,
) -> tuple[str, dict | None]:
    """Reconcile an agent ``running_update`` write operator-wins.

    Returns ``(doc_to_persist, rejected_entry_or_None)``. ``rejected_entry`` is
    a ledger dict (caller appends it to the vision ``_rejected_agent_edits``
    list) when the operator's mid-cycle body edit superseded the agent's
    document; otherwise ``None``.
    """
    agent = agent or ""
    if baseline is None:
        # No cycle baseline resolvable (legacy task) — preserve the historical
        # blind-assign behavior.
        return agent, None
    current = current or ""
    digest_lines = _detect_digest_append(baseline, current)
    if digest_lines is None:
        # The operator edited the body — operator wins; record the agent's
        # superseded document so it surfaces to next cycle's agent.
        rejected = {
            "at": _utcnow().isoformat(),
            "field": "running_update",
            "kind": RUNNING_UPDATE_REJECTED_KIND,
            "agent_value": agent,
        }
        return current, rejected
    if not digest_lines:
        # Nothing changed it since the agent got the baseline — take the
        # agent's document.
        return agent, None
    # Pure routine-digest append — take the agent's rewrite and re-integrate
    # the mid-cycle digest lines so the routine event is not lost.
    return _reintegrate_digest(agent, digest_lines), None


def apply_reconciled_running_update(
    project: Project,
    *,
    baseline: str | None,
    agent: str,
) -> dict | None:
    """Mutate ``project.running_update`` with the operator-wins reconciliation.

    When the operator won a body edit, the agent's superseded document is
    appended to the vision ``_rejected_agent_edits`` ledger (capped like the
    existing ledger) and ``project.vision`` is flagged modified. Returns the
    rejected-edit entry (or ``None``). The caller is responsible for
    ``session.add(project)`` / ``commit``.
    """
    doc, rejected = reconcile_running_update(baseline, agent, project.running_update)
    project.running_update = doc
    if rejected is not None and isinstance(project.vision, dict):
        # Mutate the live vision dict in place (append_rejected_edits returns
        # the same dict so we flag_modified once) rather than reassigning
        # project.vision. A direct ``project.vision = ...`` write would trip the
        # vision-lock invariant (test_invariants.TestVisionLockInvariant); an
        # in-place flagged mutation stays off it. Both write paths already hold
        # the project row lock (the live op via its session, the completion
        # fallback via the strategic post-commit pair-lock), so this is safe.
        append_rejected_edits(project.vision, [rejected])
        flag_modified(project, "vision")
    return rejected
