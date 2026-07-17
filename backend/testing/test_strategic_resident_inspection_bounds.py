"""Byte/BFS/revision bounds for Resident task inspection (M3 blockers 1-4).

These lock the tightened boundedness contract for
``strategic_resident_task_inspection``:

* Blocker 1 — every inspection response fits both the projected ``data`` cap and
  the complete serialized-envelope cap; oversized payloads collapse to a
  checksummed preview rather than blowing the budget.
* Blocker 2 — lineage descent is cycle-safe and bounded (no unbounded pre-LIMIT
  sibling sort; a high-fan-out parent cannot exceed ``max_tasks``).
* Blocker 3 — the exact requested root always survives both count truncation
  (many ancestors) and byte truncation (a large lineage that overflows the cap).
* Blocker 4 — the lineage revision changes whenever any *included* member's
  evidence changes, not only when the root does, and is otherwise stable.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Project, Task
from app.routers.agent_resident_capabilities import (
    MAX_CAPABILITY_REQUESTS_PER_TURN,
)
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    MAX_TURN_EVIDENCE_BYTES,
    inspect_project_task,
    inspect_project_task_lineage,
)


def _data_bytes(evidence) -> int:
    # Mirror the module's canonical encoding of the projected ``data`` payload.
    return len(
        json.dumps(
            evidence.data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _response_bytes(evidence) -> int:
    return len(evidence.model_dump_json().encode("utf-8"))


def test_turn_aggregate_ceiling_covers_max_requests():
    # The 48 KB aggregate-per-turn budget is enforced implicitly: the router caps a
    # turn to MAX_CAPABILITY_REQUESTS_PER_TURN distinct inspections, each bounded to
    # MAX_EVIDENCE_RESPONSE_BYTES. This invariant is what makes threading a running
    # total through the request path unnecessary.
    assert (
        MAX_CAPABILITY_REQUESTS_PER_TURN * MAX_EVIDENCE_RESPONSE_BYTES
        <= MAX_TURN_EVIDENCE_BYTES
    )
    # And the tightened per-response cap is genuinely small, not the old 64 KB.
    assert MAX_EVIDENCE_DATA_BYTES <= MAX_EVIDENCE_RESPONSE_BYTES <= 5_500


@pytest.mark.asyncio
async def test_single_task_evidence_respects_data_and_response_caps(db_session):
    project = Project(name="caps", user_id="u")
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="t" * 50_000,
        latest_status_message="m" * 50_000,
        status_log=[{"message": "x" * 20_000, "i": index} for index in range(40)],
        context={
            "error": "e" * 50_000,
            "failure_category": "f" * 50_000,
            "branch": "b" * 50_000,
        },
        depends_on=["d" * 400 for _ in range(80)],
    )
    db_session.add(task)
    await db_session.commit()

    for kind in ("task", "logs", "failure", "provider"):
        evidence = await inspect_project_task(
            db_session,
            project_id=project.id,
            task_id=task.id,
            kind=kind,
            log_limit=40,
        )
        assert _data_bytes(evidence) <= MAX_EVIDENCE_DATA_BYTES, kind
        assert _response_bytes(evidence) <= MAX_EVIDENCE_RESPONSE_BYTES, kind
        # Hard literal so a reverted constant OR a dropped response-cap pass is
        # caught, not silently tracked by a constant that moved with it.
        assert _response_bytes(evidence) <= 5_500, kind
        assert len(evidence.checksum) == 64


@pytest.mark.asyncio
async def test_oversized_evidence_collapses_to_checksummed_preview(db_session):
    project = Project(name="collapse", user_id="u")
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="over",
        latest_status_message="m" * 20_000,
        context={
            "error": "e" * 20_000,
            "failure_category": "cat",
            "blocked_reason": "r" * 20_000,
            "park_reason": "p" * 20_000,
        },
    )
    db_session.add(task)
    await db_session.commit()

    evidence = await inspect_project_task(
        db_session, project_id=project.id, task_id=task.id, kind="failure"
    )
    # The projected failure fields exceed the data cap, so it collapses to a bounded
    # preview that still identifies the task and fingerprints the full content.
    assert evidence.data.get("evidence_truncated") is True
    assert evidence.data["task_id"] == str(task.id)
    assert len(evidence.data["content_checksum"]) == 64
    assert _data_bytes(evidence) <= MAX_EVIDENCE_DATA_BYTES
    assert _response_bytes(evidence) <= MAX_EVIDENCE_RESPONSE_BYTES


async def _chain(db_session, project_id, length):
    """Create a linear ancestor chain root0 -> root1 -> ... -> leaf; return all."""
    tasks = []
    parent_id = None
    for index in range(length):
        task = Task(
            project_id=project_id,
            parent_task_id=parent_id,
            title=f"gen-{index}-" + "z" * 200,
            payloads=[{"type": "plan"}],
        )
        db_session.add(task)
        await db_session.flush()
        parent_id = task.id
        tasks.append(task)
    return tasks


@pytest.mark.asyncio
async def test_lineage_root_survives_many_ancestors(db_session):
    project = Project(name="deep", user_id="u")
    db_session.add(project)
    await db_session.flush()
    chain = await _chain(db_session, project.id, 60)  # > default max_tasks (50)
    await db_session.commit()
    leaf = chain[-1]

    evidence = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=leaf.id
    )
    ids = {entry["task_id"] for entry in evidence.data["tasks"]}
    assert str(leaf.id) in ids, "requested root must survive ancestor truncation"
    assert evidence.data["truncated"] is True
    assert evidence.data["requested_task_id"] == str(leaf.id)


@pytest.mark.asyncio
async def test_lineage_root_survives_byte_truncation(db_session):
    project = Project(name="bytes", user_id="u")
    db_session.add(project)
    await db_session.flush()
    # 25 ancestors: under the count cap (50) but far over the ~4.8 KB byte cap, so
    # the lineage must byte-truncate while still keeping the requested root entry.
    chain = await _chain(db_session, project.id, 26)
    await db_session.commit()
    leaf = chain[-1]

    evidence = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=leaf.id
    )
    # Root's structured entry is present (not merely swept into a preview blob).
    assert "tasks" in evidence.data
    ids = {entry["task_id"] for entry in evidence.data["tasks"]}
    assert str(leaf.id) in ids
    assert evidence.data["truncated"] is True
    assert len(evidence.data["tasks"]) < len(chain)
    assert _data_bytes(evidence) <= MAX_EVIDENCE_DATA_BYTES
    assert _response_bytes(evidence) <= MAX_EVIDENCE_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_lineage_bfs_is_cycle_safe(db_session):
    project = Project(name="cycle", user_id="u")
    db_session.add(project)
    await db_session.flush()
    a = Task(project_id=project.id, title="a", payloads=[{"type": "plan"}])
    b = Task(project_id=project.id, title="b", payloads=[{"type": "create"}])
    db_session.add_all([a, b])
    await db_session.flush()
    # Introduce a parent cycle: a -> b and b -> a.
    a.parent_task_id = b.id
    b.parent_task_id = a.id
    await db_session.commit()

    evidence = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=a.id
    )
    ids = [entry["task_id"] for entry in evidence.data["tasks"]]
    # Terminates, and no task appears twice despite the cycle.
    assert len(ids) == len(set(ids))
    assert ids.count(str(a.id)) == 1
    assert str(a.id) in ids


@pytest.mark.asyncio
async def test_lineage_wide_fanout_is_bounded(db_session):
    project = Project(name="wide", user_id="u")
    db_session.add(project)
    await db_session.flush()
    root = Task(project_id=project.id, title="root", payloads=[{"type": "plan"}])
    db_session.add(root)
    await db_session.flush()
    for index in range(40):
        db_session.add(
            Task(
                project_id=project.id,
                parent_task_id=root.id,
                title=f"child-{index}",
                payloads=[{"type": "create"}],
            )
        )
    await db_session.commit()

    evidence = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=root.id, max_tasks=10
    )
    ids = [entry["task_id"] for entry in evidence.data["tasks"]]
    assert len(ids) <= 10
    assert len(ids) == len(set(ids))
    assert str(root.id) in ids
    assert evidence.data["truncated"] is True


@pytest.mark.asyncio
async def test_lineage_truncated_subset_is_deterministic(db_session):
    project = Project(name="determinism", user_id="u")
    db_session.add(project)
    await db_session.flush()
    root = Task(project_id=project.id, title="root", payloads=[{"type": "plan"}])
    db_session.add(root)
    await db_session.flush()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Insert in REVERSE chronological order so insertion/heap order != created_at
    # order. A correct ``ORDER BY created_at`` still selects the earliest children;
    # a fetch without it would return the wrong (insertion-order) subset on SQLite,
    # so this pins the deterministic-subset contract, not just cross-call stability.
    for rank in reversed(range(20)):
        db_session.add(
            Task(
                project_id=project.id,
                parent_task_id=root.id,
                title=f"child-{rank:02d}",
                payloads=[{"type": "create"}],
                created_at=base + timedelta(seconds=rank),
            )
        )
    await db_session.commit()

    first = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=root.id, max_tasks=6
    )
    second = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=root.id, max_tasks=6
    )
    # Stable across identical calls: revision, checksum, and membership all match.
    assert first.revision == second.revision
    assert first.checksum == second.checksum
    assert [e["task_id"] for e in first.data["tasks"]] == [
        e["task_id"] for e in second.data["tasks"]
    ]
    # Deterministic earliest-by-created_at subset: root + children ranked 0..4.
    child_titles = [
        e["title"] for e in first.data["tasks"] if e["task_id"] != str(root.id)
    ]
    assert child_titles == [f"child-{rank:02d}" for rank in range(5)]
    assert first.data["truncated"] is True


@pytest.mark.asyncio
async def test_small_max_tasks_reports_descendant_truncation(db_session):
    project = Project(name="smallmax", user_id="u")
    db_session.add(project)
    await db_session.flush()
    chain = await _chain(db_session, project.id, 3)  # gp -> p -> leaf
    leaf = chain[-1]
    db_session.add(
        Task(
            project_id=project.id,
            parent_task_id=leaf.id,
            title="descendant",
            payloads=[{"type": "create"}],
        )
    )
    await db_session.commit()

    # max_tasks=3: 2 ancestors + root exactly fill the budget, so the loop never
    # runs; the root's dropped child must still be reported as truncation even
    # though there is no ancestor-count or byte truncation.
    evidence = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=leaf.id, max_tasks=3
    )
    ids = {entry["task_id"] for entry in evidence.data["tasks"]}
    assert str(leaf.id) in ids
    assert len(evidence.data["tasks"]) == 3
    assert evidence.data["truncated"] is True


@pytest.mark.asyncio
async def test_lineage_revision_tracks_included_descendant_changes(db_session):
    project = Project(name="rev", user_id="u")
    db_session.add(project)
    await db_session.flush()
    parent = Task(project_id=project.id, title="parent", payloads=[{"type": "plan"}])
    db_session.add(parent)
    await db_session.flush()
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="child",
        payloads=[{"type": "create"}],
    )
    db_session.add(child)
    await db_session.commit()

    first = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=parent.id
    )
    assert first.revision.startswith("lineage:")

    # Re-inspecting with no change yields a stable revision.
    stable = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=parent.id
    )
    assert stable.revision == first.revision

    # Mutating an included *descendant* (not the root) must change the revision.
    child.version = int(child.version) + 1
    child.title = "child-v2"
    await db_session.commit()

    after = await inspect_project_task_lineage(
        db_session, project_id=project.id, task_id=parent.id
    )
    assert after.revision != first.revision
    assert after.revision.startswith("lineage:")
