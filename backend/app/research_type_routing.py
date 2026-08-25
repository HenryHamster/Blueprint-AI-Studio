"""Routing for research findings that document instead of spawning work.

Two finding types never become implementation tasks, and correctly so:
``design_doc_observation`` ("the design doc should say X") is appended to the
project design doc's ``proposed_changes``, and ``direction_update`` ("this
direction should read Y") is written onto the target direction row as
``pending_update``.  Spawning an agent for either would be wrong.

Extracted from ``research_completion`` -- which is a chronic size offender the
governance checker refuses to bump -- so the router and the record of what it
routed live together.  That co-location is the point: the routing outcome
(*did the side effect actually land, and where?*) is only knowable here, and it
is exactly the fact the discovery decision packet needs.  Before the withheld
record existed, a routed finding reached the reviewer as nothing at all: it
gained no child task, so it carried no ``finding_hash`` into the survivors
join, so it never entered ``plan_output``, which is the only thing the packet
reads.  The packet's completeness manifest then reported
``required_evidence_omitted: false`` over an all-zero coverage, and the engine
escalated to a human for content it was holding the whole time.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, NamedTuple

from app.project_telemetry import (
    get_project_telemetry as _get_project_telemetry,
    set_project_telemetry as _set_project_telemetry,
)
from app.research_execution_authority import (
    WITHHELD_DISPOSITION_TYPE_ROUTED,
    WITHHELD_ROUTE_DESIGN_DOC,
    WITHHELD_ROUTE_DIRECTION_PENDING_UPDATE,
    build_withheld_finding_record,
)
from app.task_locking import with_design_doc_locked, with_directions_locked
from app.time_utils import _utcnow

class TypeRouteOutcome(NamedTuple):
    """What routing did with one finding.

    ``applied`` is the machine answer to "did the side effect actually land?".
    It is False for a duplicate observation already pending in the same
    section, and for a ``direction_update`` whose target direction is missing
    or gone from the project.  Those no-op cases are the strictly worse half of
    this defect: they documented nothing, incremented no counter, and did not
    even reach the parent's status string.  ``withheld_record`` is emitted for
    applied and no-op alike, distinguished only by ``routing_applied`` and its
    reason, so neither can vanish.
    """

    applied: bool
    direction_id: str | None
    withheld_record: dict[str, Any]


async def route_non_spawning_finding(
    session,
    task,
    finding: Mapping[str, Any],
    *,
    parent_direction_id: str | None,
) -> TypeRouteOutcome | None:
    """Route one finding into durable project state, or return ``None``.

    ``None`` means "not mine" -- the caller continues with its ordinary spawn
    pipeline.  Any other return means no task will be created for this finding,
    and the caller must carry ``withheld_record`` into the parent's withheld
    set so the decision packet can show it.
    """

    finding_type = finding.get("type", "improvement")
    if finding_type == "design_doc_observation":
        return await _route_design_doc_observation(
            session, task, finding, parent_direction_id=parent_direction_id
        )
    if finding_type == "direction_update":
        return await _route_direction_update(
            session, task, finding, parent_direction_id=parent_direction_id
        )
    return None


async def _route_design_doc_observation(
    session,
    task,
    finding: Mapping[str, Any],
    *,
    parent_direction_id: str | None,
) -> TypeRouteOutcome:
    # Append to design doc proposals for Phase 1 to review
    _obs_text = finding.get("description", "")[:500]
    _obs_section = finding.get("section", "general")
    _obs_entry = {
        "id": f"pc-{uuid.uuid4().hex[:8]}",
        "observation": _obs_text,
        "section": _obs_section,
        "proposed_by_task": str(task.id),
        "proposed_at": _utcnow().isoformat(),
        "status": "pending",
    }

    _captured_proposals: dict = {}
    _obs_applied = {"value": False}

    # Pre-read current proposals from telemetry so the lock callback
    # sees the canonical data after strip_migrated_doc_keys removed
    # this key from design_doc.
    _existing_proposals = await _get_project_telemetry(
        session, task.project_id, "proposed_changes",
        design_doc_fallback=None,
    )

    def _add_obs_proposal(
        doc: dict,
        _existing=_existing_proposals,
        _text=_obs_text,
        _section=_obs_section,
        _entry=_obs_entry,
        _applied=_obs_applied,
        _captured=_captured_proposals,
    ) -> None:
        # Hydrate from telemetry before mutation.
        proposals = list(_existing or doc.get("proposed_changes", []))
        # Dedup: skip if identical observation already exists
        existing = next(
            (p for p in proposals
             if p.get("observation") == _text and p.get("section") == _section),
            None
        )
        if not existing:
            proposals.append(_entry)
            _applied["value"] = True
        doc["proposed_changes"] = proposals[-30:]  # cap at 30 pending
        _captured["value"] = doc["proposed_changes"]

    await with_design_doc_locked(session, task.project_id, _add_obs_proposal)
    if _captured_proposals.get("value") is not None:
        await _set_project_telemetry(
            session, task.project_id, "proposed_changes", _captured_proposals["value"]
        )
    applied = bool(_obs_applied["value"])
    direction_id = finding.get("direction_id") or parent_direction_id
    return TypeRouteOutcome(
        applied=applied,
        direction_id=direction_id,
        withheld_record=build_withheld_finding_record(
            finding,
            disposition=WITHHELD_DISPOSITION_TYPE_ROUTED,
            reason=(
                "design_doc_observation appended to the project design doc's"
                " proposed_changes"
                if applied
                else "design_doc_observation no-op: an identical observation is"
                " already pending in this section"
            ),
            finding_type="design_doc_observation",
            routed_to=WITHHELD_ROUTE_DESIGN_DOC,
            destination_id=_obs_entry["id"] if applied else None,
            direction_id=direction_id,
            routing_applied=applied,
        ),
    )


async def _route_direction_update(
    session,
    task,
    finding: Mapping[str, Any],
    *,
    parent_direction_id: str | None,
) -> TypeRouteOutcome:
    # Process as a direction modification proposal, not a task
    target_id = finding.get("direction_id")
    if not target_id:
        # direction_update requires a target direction_id
        return TypeRouteOutcome(
            applied=False,
            direction_id=parent_direction_id,
            withheld_record=build_withheld_finding_record(
                finding,
                disposition=WITHHELD_DISPOSITION_TYPE_ROUTED,
                reason=(
                    "direction_update no-op: the finding named no target"
                    " direction_id"
                ),
                finding_type="direction_update",
                routed_to=WITHHELD_ROUTE_DIRECTION_PENDING_UPDATE,
                direction_id=parent_direction_id,
                routing_applied=False,
            ),
        )
    _du_finding = finding
    _du_target_id = target_id
    _du_task_id = str(task.id)
    _du_applied = {"value": False}

    def _set_pending_update(
        dirs: list,
        _target_id=_du_target_id,
        _update=_du_finding,
        _task_id=_du_task_id,
        _applied=_du_applied,
    ) -> None:
        for d in dirs:
            if d.get("id") == _target_id:
                d["pending_update"] = {
                    "suggested_text": _update.get(
                        "suggested_text", d.get("text")
                    ),
                    "suggested_priority": _update.get(
                        "suggested_priority", d.get("priority")
                    ),
                    "suggested_paths": _update.get(
                        "suggested_paths", d.get("paths", [])
                    ),
                    "rationale": _update.get("description", "")[:300],
                    "proposed_at": _utcnow().isoformat(),
                    "proposed_by": _task_id,
                }
                _applied["value"] = True
                break

    await with_directions_locked(session, task.project_id, _set_pending_update)
    applied = bool(_du_applied["value"])
    direction_id = finding.get("direction_id") or parent_direction_id
    return TypeRouteOutcome(
        applied=applied,
        direction_id=direction_id,
        withheld_record=build_withheld_finding_record(
            finding,
            disposition=WITHHELD_DISPOSITION_TYPE_ROUTED,
            reason=(
                f"direction_update written to direction {target_id} as"
                " pending_update"
                if applied
                else f"direction_update no-op: target direction {target_id} is"
                " not on the project"
            ),
            finding_type="direction_update",
            routed_to=WITHHELD_ROUTE_DIRECTION_PENDING_UPDATE,
            destination_id=target_id if applied else None,
            direction_id=direction_id,
            routing_applied=applied,
        ),
    )
