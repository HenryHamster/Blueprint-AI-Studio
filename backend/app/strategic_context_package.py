"""Strategic context package read model for the operator workbench.

The package is a bounded projection over existing project, task, artifact, and
knowledge-document state. It is not a writable canvas system and deliberately
omits heavy fields such as knowledge bodies and artifact logs.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import Artifact, KnowledgeDocument, Project, StrategicCycle, Task


def _compact_text(value: Any, *, max_chars: int = 360) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _isoformat_source_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return _compact_text(value, max_chars=80)


def _context_source(kind: str, count: int) -> Dict[str, Any] | None:
    if count <= 0:
        return None
    return {"kind": kind, "count": int(count)}


def _canvas_node(
    node_id: str,
    *,
    kind: str,
    label: Any,
    summary: Any = None,
    status: Any = None,
    count: int | None = None,
    target: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    node = {
        "id": node_id,
        "kind": kind,
        "label": _compact_text(label, max_chars=90) or kind.replace("_", " "),
    }
    if summary is not None:
        node["summary"] = _compact_text(summary, max_chars=180)
    if status is not None:
        node["status"] = _compact_text(status, max_chars=60)
    if count is not None:
        node["count"] = int(count)
    if target:
        node["target"] = target
    return node


def _canvas_edge(
    source: str,
    target: str,
    *,
    label: str | None = None,
) -> Dict[str, Any]:
    edge = {"source": source, "target": target}
    if label:
        edge["label"] = label
    return edge


def _artifact_context_kind(artifact: Artifact, *, has_logs: bool) -> str:
    if artifact.pr_url:
        return "pull_request"
    if artifact.storage_uri:
        return "storage"
    if artifact.commit_hash:
        return "commit"
    if has_logs:
        return "log"
    return "artifact"


async def load_strategic_context_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> Dict[str, Any]:
    """Return bounded durable context descriptors without heavy bodies/logs."""

    knowledge_total = (
        await session.execute(
            select(func.count()).where(KnowledgeDocument.project_id == project_id)
        )
    ).scalar()
    knowledge_rows = (
        await session.execute(
            select(KnowledgeDocument)
            .options(
                load_only(
                    KnowledgeDocument.id,
                    KnowledgeDocument.title,
                    KnowledgeDocument.summary,
                    KnowledgeDocument.source_kind,
                    KnowledgeDocument.topic,
                    KnowledgeDocument.direction_id,
                    KnowledgeDocument.source_task_id,
                    KnowledgeDocument.created_at,
                    KnowledgeDocument.updated_at,
                )
            )
            .where(KnowledgeDocument.project_id == project_id)
            .order_by(
                KnowledgeDocument.updated_at.desc(),
                KnowledgeDocument.created_at.desc(),
            )
            .limit(4)
        )
    ).scalars().all()

    meaningful_artifact_filter = or_(
        Artifact.pr_url.is_not(None),
        Artifact.storage_uri.is_not(None),
        Artifact.commit_hash.is_not(None),
        Artifact.logs.is_not(None),
    )
    artifact_total = (
        await session.execute(
            select(func.count())
            .select_from(Artifact)
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Task.project_id == project_id,
                Task.archived.is_(False),
                meaningful_artifact_filter,
            )
        )
    ).scalar()
    artifact_rows = (
        await session.execute(
            select(
                Artifact,
                Task.id,
                Task.title,
                Task.status,
                Artifact.logs.is_not(None).label("has_logs"),
            )
            .options(
                load_only(
                    Artifact.id,
                    Artifact.task_id,
                    Artifact.artifact_type,
                    Artifact.commit_hash,
                    Artifact.pr_url,
                    Artifact.storage_uri,
                    Artifact.logical_key,
                    Artifact.created_at,
                )
            )
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Task.project_id == project_id,
                Task.archived.is_(False),
                meaningful_artifact_filter,
            )
            .order_by(Artifact.created_at.desc())
            .limit(4)
        )
    ).all()

    knowledge_items = [
        {
            "id": str(doc.id),
            "kind": "knowledge_document",
            "label": _compact_text(doc.title, max_chars=100),
            "summary": _compact_text(doc.summary, max_chars=180),
            "source_kind": _compact_text(doc.source_kind, max_chars=60),
            "topic": _compact_text(doc.topic, max_chars=80),
            "direction_id": _compact_text(doc.direction_id, max_chars=80),
            "source_task_id": str(doc.source_task_id) if doc.source_task_id else None,
            "target": {
                "surface": "knowledge",
                "document_id": str(doc.id),
                "direction_id": _compact_text(doc.direction_id, max_chars=80),
                "task_id": str(doc.source_task_id) if doc.source_task_id else None,
            },
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        }
        for doc in knowledge_rows
    ]
    artifact_items = []
    for artifact, task_id, task_title, task_status, has_logs in artifact_rows:
        artifact_type = getattr(
            artifact.artifact_type, "value", artifact.artifact_type
        )
        context_kind = _artifact_context_kind(artifact, has_logs=bool(has_logs))
        label = (
            artifact.logical_key
            or artifact_type
            or "Artifact"
        )
        artifact_items.append(
            {
                "id": str(artifact.id),
                "kind": context_kind,
                "label": _compact_text(label, max_chars=100),
                "artifact_type": _compact_text(artifact_type, max_chars=80),
                "task_id": str(task_id) if task_id else None,
                "task_title": _compact_text(task_title, max_chars=120),
                "task_status": getattr(task_status, "value", task_status),
                "logical_key": _compact_text(artifact.logical_key, max_chars=160),
                "target": {
                    "surface": "task",
                    "task_id": str(task_id) if task_id else None,
                    "artifact_id": str(artifact.id),
                },
                "created_at": (
                    artifact.created_at.isoformat() if artifact.created_at else None
                ),
            }
        )

    return {
        "knowledge": {
            "total": int(knowledge_total or 0),
            "items": knowledge_items,
        },
        "artifacts": {
            "total": int(artifact_total or 0),
            "items": artifact_items,
        },
    }


def _strategic_context_canvas(
    project: Project,
    *,
    title: str,
    active_cycle: Dict[str, Any] | None,
    active_task: Task | None,
    latest_cycle: StrategicCycle | None,
    running_update: Dict[str, Any],
    problem_model: Dict[str, Any],
    directions: Dict[str, Any],
    message_counts: Dict[str, int],
    task_counts: Dict[str, int],
    evidence: Dict[str, Any],
    drilldowns: List[Dict[str, Any]],
) -> Dict[str, Any]:
    nodes: list[Dict[str, Any]] = [
        _canvas_node(
            f"project:{project.id}",
            kind="project",
            label=project.name,
            summary=title,
            target={"surface": "project", "section": "running-update"},
        )
    ]
    edges: list[Dict[str, Any]] = []

    def add_project_child(node: Dict[str, Any], label: str) -> None:
        nodes.append(node)
        edges.append(_canvas_edge(f"project:{project.id}", node["id"], label=label))

    if running_update.get("char_count"):
        add_project_child(
            _canvas_node(
                "running_update",
                kind="running_update",
                label="Running update",
                summary=running_update.get("excerpt"),
                target={"surface": "project", "section": "running-update"},
            ),
            "summarizes",
        )
    add_project_child(
        _canvas_node(
            "problem_model",
            kind="problem_model",
            label=problem_model.get("current_focus") or "Problem model",
            summary=problem_model.get("purpose"),
            count=int(problem_model.get("active_gap_count") or 0),
            target={"surface": "project", "section": "problem-model"},
        ),
        "frames",
    )
    top_gap = problem_model.get("top_gap") or {}
    if isinstance(top_gap, dict) and top_gap.get("id"):
        gap_node_id = f"gap:{top_gap.get('id')}"
        nodes.append(
            _canvas_node(
                gap_node_id,
                kind="gap",
                label=top_gap.get("name"),
                status=top_gap.get("status"),
                target={"surface": "project", "section": "problem-model"},
            )
        )
        edges.append(_canvas_edge("problem_model", gap_node_id, label="top gap"))
    primary_direction = (
        directions.get("primary") if isinstance(directions, dict) else None
    )
    if isinstance(primary_direction, dict) and primary_direction.get("id"):
        add_project_child(
            _canvas_node(
                f"direction:{primary_direction.get('id')}",
                kind="direction",
                label=primary_direction.get("text"),
                status=primary_direction.get("status"),
                target={"surface": "project", "section": "directions"},
            ),
            "directs",
        )
    if active_cycle:
        add_project_child(
            _canvas_node(
                f"cycle:{active_cycle.get('id')}",
                kind="strategic_cycle",
                label="Active Strategic cycle",
                summary=active_cycle.get("summary_text"),
                status=active_cycle.get("outcome"),
                target={"surface": "project", "section": "running-update"},
            ),
            "runs",
        )
    elif latest_cycle:
        add_project_child(
            _canvas_node(
                f"cycle:{latest_cycle.id}",
                kind="strategic_cycle",
                label="Latest Strategic cycle",
                summary=latest_cycle.summary_text,
                status=latest_cycle.outcome,
                target={"surface": "project", "section": "running-update"},
            ),
            "last cycle",
        )
    if active_task:
        task_node_id = f"task:{active_task.id}"
        nodes.append(
            _canvas_node(
                task_node_id,
                kind="task",
                label=active_task.title or active_task.first_payload.get("type"),
                summary=active_task.latest_status_message,
                status=getattr(active_task.status, "value", active_task.status),
                target={"surface": "task", "task_id": str(active_task.id)},
            )
        )
        parent = (
            f"cycle:{active_cycle.get('id')}"
            if active_cycle and active_cycle.get("id")
            else f"project:{project.id}"
        )
        edges.append(_canvas_edge(parent, task_node_id, label="executes"))
    open_messages = int(message_counts.get("open") or 0)
    linked_messages = int(message_counts.get("linked") or 0)
    if open_messages or linked_messages:
        add_project_child(
            _canvas_node(
                "messages",
                kind="messages",
                label="Messages",
                count=open_messages + linked_messages,
                target={"surface": "messages"},
            ),
            "operator input",
        )
    if int(task_counts.get("total") or 0) > 0:
        add_project_child(
            _canvas_node(
                "pipeline",
                kind="pipeline",
                label="Pipeline",
                count=int(task_counts.get("total") or 0),
                target={"surface": "pipeline"},
            ),
            "work queue",
        )

    for doc in (evidence.get("knowledge") or {}).get("items", [])[:3]:
        node_id = f"knowledge:{doc.get('id')}"
        nodes.append(
            _canvas_node(
                node_id,
                kind="knowledge_document",
                label=doc.get("label"),
                summary=doc.get("summary"),
                status=doc.get("source_kind"),
                target=doc.get("target"),
            )
        )
        direction_id = doc.get("direction_id")
        parent = (
            f"direction:{direction_id}"
            if direction_id
            and any(n["id"] == f"direction:{direction_id}" for n in nodes)
            else f"project:{project.id}"
        )
        edges.append(_canvas_edge(parent, node_id, label="knowledge"))
    for artifact in (evidence.get("artifacts") or {}).get("items", [])[:3]:
        node_id = f"artifact:{artifact.get('id')}"
        nodes.append(
            _canvas_node(
                node_id,
                kind="artifact",
                label=artifact.get("label") or artifact.get("artifact_type"),
                summary=artifact.get("task_title"),
                status=artifact.get("artifact_type"),
                target=artifact.get("target"),
            )
        )
        task_id = artifact.get("task_id")
        parent = (
            f"task:{task_id}"
            if task_id and any(n["id"] == f"task:{task_id}" for n in nodes)
            else "pipeline"
            if any(n["id"] == "pipeline" for n in nodes)
            else f"project:{project.id}"
        )
        edges.append(_canvas_edge(parent, node_id, label="artifact"))

    return {
        "id": f"strategic-canvas:{project.id}",
        "title": "Strategic context map",
        "kind": "strategic_context",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes[:16],
        "edges": edges[:24],
        "drilldowns": drilldowns,
    }


def build_strategic_context_package_summary(
    project: Project,
    *,
    active_cycle: Dict[str, Any] | None,
    active_task: Task | None,
    latest_cycle: StrategicCycle | None,
    running_update: Dict[str, Any],
    problem_model: Dict[str, Any],
    directions: Dict[str, Any],
    message_counts: Dict[str, int],
    task_counts: Dict[str, int],
    attention: Dict[str, Any] | None,
    evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Describe the current Strategic context without embedding large source docs."""

    attention_counts = (
        attention.get("counts", {}) if isinstance(attention, dict) else {}
    )
    model_count = (
        int(problem_model.get("active_gap_count") or 0)
        + int(problem_model.get("capability_count") or 0)
        + int(problem_model.get("pending_confirmation_count") or 0)
    )
    attention_count = int(attention_counts.get("needs_operator") or 0) + int(
        attention_counts.get("watch") or 0
    )
    evidence = evidence or {
        "knowledge": {"total": 0, "items": []},
        "artifacts": {"total": 0, "items": []},
    }
    knowledge_total = int((evidence.get("knowledge") or {}).get("total") or 0)
    artifact_total = int((evidence.get("artifacts") or {}).get("total") or 0)
    sources = [
        source
        for source in [
            _context_source(
                "running_update",
                1 if running_update.get("char_count") else 0,
            ),
            _context_source("problem_model", model_count),
            _context_source("directions", int(directions.get("active_count") or 0)),
            _context_source(
                "messages",
                int(message_counts.get("open") or 0)
                + int(message_counts.get("linked") or 0),
            ),
            _context_source("tasks", int(task_counts.get("total") or 0)),
            _context_source("attention", attention_count),
            _context_source("knowledge", knowledge_total),
            _context_source("artifacts", artifact_total),
            _context_source("design", 1 if project.design_doc else 0),
        ]
        if source is not None
    ]
    source_item_count = sum(int(source["count"]) for source in sources)
    title = (
        problem_model.get("current_focus")
        or problem_model.get("purpose")
        or ((directions.get("primary") or {}).get("text") if directions else None)
        or project.name
    )
    package_title = _compact_text(title, max_chars=100) or "Strategic context"
    active_stale = bool(active_cycle and active_cycle.get("stale"))
    status = (
        "stale"
        if active_stale
        else "building"
        if active_task is not None or bool((project.vision or {}).get("_strategic_refresh"))
        else "ready"
    )
    latest_time = (
        _isoformat_source_time(active_task.updated_at if active_task else None)
        or _isoformat_source_time(active_task.started_at if active_task else None)
        or _isoformat_source_time(latest_cycle.completed_at if latest_cycle else None)
        or _isoformat_source_time(problem_model.get("last_updated"))
        or _isoformat_source_time(project.created_at)
    )
    drilldowns = [
        {
            "id": "running_update",
            "label": "Update",
            "target": {"surface": "project", "section": "running-update"},
        },
        {
            "id": "problem_model",
            "label": "Model",
            "target": {"surface": "project", "section": "problem-model"},
        },
        {
            "id": "directions",
            "label": "Directions",
            "target": {"surface": "project", "section": "directions"},
        },
        {
            "id": "pipeline",
            "label": "Pipeline",
            "target": {"surface": "pipeline"},
        },
        {
            "id": "runtime",
            "label": "Runtime",
            "target": {"surface": "runtime", "section": "health"},
        },
    ]
    if project.design_doc:
        drilldowns.append(
            {
                "id": "design",
                "label": "Design",
                "target": {"surface": "project", "section": "design-doc"},
            }
        )

    canvas = _strategic_context_canvas(
        project,
        title=package_title,
        active_cycle=active_cycle,
        active_task=active_task,
        latest_cycle=latest_cycle,
        running_update=running_update,
        problem_model=problem_model,
        directions=directions,
        message_counts=message_counts,
        task_counts=task_counts,
        evidence=evidence,
        drilldowns=drilldowns,
    )
    knowledge_resources = [
        {
            "id": f"knowledge:{doc.get('id')}",
            "kind": "knowledge_document",
            "label": doc.get("label") or "Knowledge document",
            "summary": doc.get("summary"),
            "target": doc.get("target"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }
        for doc in (evidence.get("knowledge") or {}).get("items", [])[:3]
    ]
    artifact_resources = [
        {
            "id": f"artifact:{artifact.get('id')}",
            "kind": "artifact",
            "label": artifact.get("label") or artifact.get("artifact_type") or "Artifact",
            "summary": artifact.get("task_title"),
            "target": artifact.get("target"),
            "created_at": artifact.get("created_at"),
        }
        for artifact in (evidence.get("artifacts") or {}).get("items", [])[:3]
    ]
    mixed_resources: list[Dict[str, Any]] = []
    for index in range(max(len(knowledge_resources), len(artifact_resources))):
        if index < len(knowledge_resources):
            mixed_resources.append(knowledge_resources[index])
        if index < len(artifact_resources):
            mixed_resources.append(artifact_resources[index])
    resources = [
        {
            "id": canvas["id"],
            "kind": "canvas",
            "label": canvas["title"],
            "summary": f"{canvas['node_count']} nodes / {canvas['edge_count']} links",
            "target": {"surface": "project", "section": "problem-model"},
        },
        *mixed_resources,
    ][:7]

    return {
        "id": f"strategic-context:{project.id}",
        "title": package_title,
        "status": status,
        "updated_at": latest_time,
        "token_count": None,
        "item_count": source_item_count,
        "sources": sources,
        "canvas": canvas,
        "resources": resources,
        "evidence": evidence,
        "drilldowns": drilldowns,
    }
