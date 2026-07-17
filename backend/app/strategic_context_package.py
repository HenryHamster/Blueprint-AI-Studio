"""Strategic context package read model for the operator workbench.

The package is a bounded projection over existing project, task, artifact, and
knowledge-document state. It is not a writable canvas system and deliberately
omits heavy fields such as knowledge bodies and artifact logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List

from app.time_utils import _utcnow

from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import (
    Artifact,
    ComputeJob,
    ComputeJobStatus,
    Direction,
    KnowledgeDocument,
    Project,
    ProjectMessage,
    SourceKind,
    StrategicCycle,
    Task,
)
from app.storage import (
    generate_download_signed_url,
    verify_artifact_project_ownership,
    viewable_content_type_for_filename,
)
from app.workbench_contracts import normalize_target

logger = logging.getLogger(__name__)


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
    normalized_target = normalize_target(target)
    if normalized_target:
        node["target"] = normalized_target
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


_REF_PREFIX_BY_KIND = {
    "artifact": "artifact",
    "direction": "direction",
    "knowledge_document": "knowledge",
    "message": "message",
    "strategic_cycle": "cycle",
    "task": "task",
}

_SELECTED_REF_DESCRIPTOR_LIMIT = 12


def _ref_identity_values(kind: str, ref_id: str) -> set[str]:
    values = {ref_id}
    prefix = _REF_PREFIX_BY_KIND.get(kind)
    if prefix:
        marker = f"{prefix}:"
        if ref_id.startswith(marker):
            values.add(ref_id[len(marker):])
        else:
            values.add(f"{marker}{ref_id}")
    return values


def _prefixed_ref_id(kind: str, ref_id: str) -> str:
    prefix = _REF_PREFIX_BY_KIND.get(kind)
    if not prefix:
        return ref_id
    marker = f"{prefix}:"
    return ref_id if ref_id.startswith(marker) else f"{marker}{ref_id}"


def _unprefixed_ref_id(kind: str, ref_id: str) -> str:
    prefix = _REF_PREFIX_BY_KIND.get(kind)
    if not prefix:
        return ref_id
    marker = f"{prefix}:"
    return ref_id[len(marker):] if ref_id.startswith(marker) else ref_id


def _fallback_ref_target(kind: str, ref_id: str) -> Dict[str, Any] | None:
    if kind == "canvas":
        return {"surface": "project", "section": "problem-model"}
    return None


def _with_target(payload: Dict[str, Any], target: Dict[str, Any] | None) -> Dict[str, Any]:
    normalized_target = normalize_target(target)
    if normalized_target:
        payload["target"] = normalized_target
    return payload


def _drilldown(drilldown_id: str, label: str, target: Dict[str, Any]) -> Dict[str, Any]:
    return _with_target({"id": drilldown_id, "label": label}, target)


def _candidate_descriptor(
    *,
    kind: str,
    ref_id: Any,
    label: Any = None,
    summary: Any = None,
    status: Any = None,
    target: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    candidate_id = _compact_text(ref_id, max_chars=160)
    candidate_kind = _compact_text(kind, max_chars=40)
    if not candidate_kind or not candidate_id:
        return None
    descriptor: Dict[str, Any] = {
        "kind": candidate_kind,
        "id": candidate_id,
        "label": _compact_text(label, max_chars=180)
        or f"{candidate_kind}:{candidate_id}",
    }
    compact_summary = _compact_text(summary, max_chars=220)
    if compact_summary:
        descriptor["summary"] = compact_summary
    compact_status = _compact_text(status, max_chars=80)
    if compact_status:
        descriptor["status"] = compact_status
    normalized_target = normalize_target(target)
    if normalized_target:
        descriptor["target"] = normalized_target
    return descriptor


def _descriptor_lookup_key(kind: Any, ref_id: Any) -> tuple[str, str] | None:
    compact_kind = _compact_text(kind, max_chars=40)
    compact_id = _compact_text(ref_id, max_chars=160)
    if not compact_kind or not compact_id:
        return None
    return compact_kind, compact_id


def _parse_uuid_ref(kind: str, ref_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(_unprefixed_ref_id(kind, ref_id))
    except (TypeError, ValueError):
        return None


def _store_resolved_descriptor(
    resolved: dict[tuple[str, str], Dict[str, Any]],
    descriptor: Dict[str, Any] | None,
) -> None:
    if not descriptor:
        return
    key = _descriptor_lookup_key(descriptor.get("kind"), descriptor.get("id"))
    if key and key not in resolved:
        resolved[key] = descriptor


def _merge_descriptor_fields(
    descriptor: Dict[str, Any],
    source: Dict[str, Any] | None,
    *,
    overwrite_label: bool = False,
) -> None:
    if not isinstance(source, dict):
        return
    if overwrite_label:
        descriptor["label"] = _compact_text(
            source.get("label") or descriptor["label"],
            max_chars=180,
        )
    summary = _compact_text(source.get("summary"), max_chars=220)
    if summary and not descriptor.get("summary"):
        descriptor["summary"] = summary
    target = normalize_target(source.get("target"))
    if target and not descriptor.get("target"):
        descriptor["target"] = target
    status = _compact_text(source.get("status"), max_chars=80)
    if status and not descriptor.get("status"):
        descriptor["status"] = status


async def resolve_selected_ref_descriptors(
    session: AsyncSession,
    project_id: uuid.UUID,
    refs: List[Dict[str, Any]],
    *,
    limit: int = _SELECTED_REF_DESCRIPTOR_LIMIT,
) -> dict[tuple[str, str], Dict[str, Any]]:
    """Resolve selected refs with small batched lookups and no heavy bodies/logs."""

    buckets: dict[str, list[tuple[str, uuid.UUID | str]]] = {
        "artifact": [],
        "direction": [],
        "knowledge_document": [],
        "message": [],
        "task": [],
    }
    seen: set[tuple[str, str]] = set()
    for ref in refs[:limit]:
        if not isinstance(ref, dict):
            continue
        key = _descriptor_lookup_key(ref.get("kind"), ref.get("id"))
        if not key or key in seen:
            continue
        seen.add(key)
        kind, ref_id = key
        if kind not in buckets:
            continue
        if kind == "direction":
            buckets[kind].append((ref_id, _unprefixed_ref_id(kind, ref_id)))
            continue
        parsed = _parse_uuid_ref(kind, ref_id)
        if parsed:
            buckets[kind].append((ref_id, parsed))

    resolved: dict[tuple[str, str], Dict[str, Any]] = {}

    task_ids = [parsed for _ref_id, parsed in buckets["task"]]
    if task_ids:
        rows = (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.title,
                        Task.latest_status_message,
                        Task.status,
                        raiseload=True,
                    )
                )
                .where(Task.project_id == project_id, Task.id.in_(task_ids))
            )
        ).scalars().all()
        by_id = {task.id: task for task in rows}
        for ref_id, parsed in buckets["task"]:
            task = by_id.get(parsed)
            if not task:
                continue
            _store_resolved_descriptor(
                resolved,
                _candidate_descriptor(
                    kind="task",
                    ref_id=ref_id,
                    label=task.title,
                    summary=task.latest_status_message,
                    status=getattr(task.status, "value", task.status),
                    target={"surface": "task", "task_id": str(task.id)},
                ),
            )

    message_ids = [parsed for _ref_id, parsed in buckets["message"]]
    if message_ids:
        rows = (
            await session.execute(
                select(ProjectMessage)
                .options(
                    load_only(
                        ProjectMessage.id,
                        ProjectMessage.message_type,
                        ProjectMessage.from_type,
                        ProjectMessage.status,
                        raiseload=True,
                    )
                )
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.id.in_(message_ids),
                )
            )
        ).scalars().all()
        by_id = {message.id: message for message in rows}
        for ref_id, parsed in buckets["message"]:
            message = by_id.get(parsed)
            if not message:
                continue
            message_type = getattr(
                message.message_type, "value", message.message_type
            )
            _store_resolved_descriptor(
                resolved,
                _candidate_descriptor(
                    kind="message",
                    ref_id=ref_id,
                    label=f"{message_type or 'message'} from {message.from_type}",
                    status=message.status,
                    target={"surface": "messages", "message_id": str(message.id)},
                ),
            )

    direction_keys = [parsed for _ref_id, parsed in buckets["direction"]]
    if direction_keys:
        rows = (
            await session.execute(
                select(Direction)
                .options(
                    load_only(
                        Direction.direction_key,
                        Direction.text,
                        Direction.status,
                        Direction.priority,
                        raiseload=True,
                    )
                )
                .where(
                    Direction.project_id == project_id,
                    Direction.direction_key.in_(direction_keys),
                )
            )
        ).scalars().all()
        by_key = {direction.direction_key: direction for direction in rows}
        for ref_id, parsed in buckets["direction"]:
            direction = by_key.get(parsed)
            if not direction:
                continue
            _store_resolved_descriptor(
                resolved,
                _candidate_descriptor(
                    kind="direction",
                    ref_id=ref_id,
                    label=direction.text or direction.direction_key,
                    summary=direction.priority,
                    status=direction.status,
                    target={
                        "surface": "project",
                        "section": "directions",
                        "direction_id": direction.direction_key,
                    },
                ),
            )

    knowledge_ids = [parsed for _ref_id, parsed in buckets["knowledge_document"]]
    if knowledge_ids:
        rows = (
            await session.execute(
                select(KnowledgeDocument)
                .options(
                    load_only(
                        KnowledgeDocument.id,
                        KnowledgeDocument.title,
                        KnowledgeDocument.summary,
                        KnowledgeDocument.source_kind,
                        KnowledgeDocument.direction_id,
                        KnowledgeDocument.source_task_id,
                        raiseload=True,
                    )
                )
                .where(
                    KnowledgeDocument.project_id == project_id,
                    KnowledgeDocument.id.in_(knowledge_ids),
                )
            )
        ).scalars().all()
        by_id = {doc.id: doc for doc in rows}
        for ref_id, parsed in buckets["knowledge_document"]:
            doc = by_id.get(parsed)
            if not doc:
                continue
            _store_resolved_descriptor(
                resolved,
                _candidate_descriptor(
                    kind="knowledge_document",
                    ref_id=ref_id,
                    label=doc.title,
                    summary=doc.summary,
                    status=doc.source_kind,
                    target={
                        "surface": "knowledge",
                        "document_id": str(doc.id),
                        "direction_id": _compact_text(
                            doc.direction_id,
                            max_chars=80,
                        ),
                        "task_id": str(doc.source_task_id)
                        if doc.source_task_id
                        else None,
                    },
                ),
            )

    artifact_ids = [parsed for _ref_id, parsed in buckets["artifact"]]
    if artifact_ids:
        rows = (
            await session.execute(
                select(Artifact, Task.id, Task.title, Task.status)
                .options(
                    load_only(
                        Artifact.id,
                        Artifact.artifact_type,
                        Artifact.logical_key,
                        Artifact.task_id,
                        raiseload=True,
                    )
                )
                .join(Task, Artifact.task_id == Task.id)
                .where(
                    Task.project_id == project_id,
                    Artifact.id.in_(artifact_ids),
                )
            )
        ).all()
        by_id = {
            artifact.id: (artifact, task_id, task_title, task_status)
            for artifact, task_id, task_title, task_status in rows
        }
        for ref_id, parsed in buckets["artifact"]:
            row = by_id.get(parsed)
            if not row:
                continue
            artifact, task_id, task_title, task_status = row
            artifact_type = getattr(
                artifact.artifact_type,
                "value",
                artifact.artifact_type,
            )
            _store_resolved_descriptor(
                resolved,
                _candidate_descriptor(
                    kind="artifact",
                    ref_id=ref_id,
                    label=artifact.logical_key or artifact_type,
                    summary=task_title,
                    status=artifact_type or getattr(task_status, "value", task_status),
                    target={
                        "surface": "task",
                        "task_id": str(task_id),
                        "artifact_id": str(artifact.id),
                    },
                ),
            )

    return resolved


def _context_package_descriptor_candidates(
    context_package: Dict[str, Any] | None,
) -> list[Dict[str, Any]]:
    if not isinstance(context_package, dict):
        return []

    candidates: list[Dict[str, Any]] = []

    resources = context_package.get("resources")
    if isinstance(resources, list):
        candidates.extend(entry for entry in resources if isinstance(entry, dict))

    canvas = context_package.get("canvas")
    if isinstance(canvas, dict):
        nodes = canvas.get("nodes")
        if isinstance(nodes, list):
            candidates.extend(entry for entry in nodes if isinstance(entry, dict))

    evidence = context_package.get("evidence")
    if isinstance(evidence, dict):
        knowledge = evidence.get("knowledge")
        if isinstance(knowledge, dict):
            for doc in (knowledge.get("items") or [])[:8]:
                if not isinstance(doc, dict):
                    continue
                doc_id = _compact_text(doc.get("id"), max_chars=140)
                if not doc_id:
                    continue
                descriptor = _candidate_descriptor(
                    kind="knowledge_document",
                    ref_id=_prefixed_ref_id("knowledge_document", doc_id),
                    label=doc.get("label") or doc.get("topic"),
                    summary=doc.get("summary"),
                    status=doc.get("source_kind"),
                    target=doc.get("target"),
                )
                if descriptor:
                    candidates.append(descriptor)
                source_task_id = doc.get("source_task_id")
                if source_task_id:
                    task_descriptor = _candidate_descriptor(
                        kind="task",
                        ref_id=_prefixed_ref_id("task", str(source_task_id)),
                        label=doc.get("label") or "Knowledge source task",
                        summary=doc.get("summary"),
                        status="knowledge source",
                        target={"surface": "task", "task_id": str(source_task_id)},
                    )
                    if task_descriptor:
                        candidates.append(task_descriptor)

        artifacts = evidence.get("artifacts")
        if isinstance(artifacts, dict):
            for artifact in (artifacts.get("items") or [])[:8]:
                if not isinstance(artifact, dict):
                    continue
                artifact_id = _compact_text(artifact.get("id"), max_chars=140)
                if not artifact_id:
                    continue
                descriptor = _candidate_descriptor(
                    kind="artifact",
                    ref_id=_prefixed_ref_id("artifact", artifact_id),
                    label=artifact.get("label")
                    or artifact.get("logical_key")
                    or artifact.get("artifact_type"),
                    summary=artifact.get("task_title"),
                    status=artifact.get("artifact_type") or artifact.get("task_status"),
                    target=artifact.get("target"),
                )
                if descriptor:
                    candidates.append(descriptor)
                task_id = artifact.get("task_id")
                if task_id:
                    task_descriptor = _candidate_descriptor(
                        kind="task",
                        ref_id=_prefixed_ref_id("task", str(task_id)),
                        label=artifact.get("task_title") or "Artifact task",
                        summary=artifact.get("label") or artifact.get("artifact_type"),
                        status=artifact.get("task_status"),
                        target={"surface": "task", "task_id": str(task_id)},
                    )
                    if task_descriptor:
                        candidates.append(task_descriptor)

    return candidates


def selected_ref_descriptor(
    ref: Dict[str, Any],
    context_package: Dict[str, Any] | None,
    resolved_descriptors: dict[tuple[str, str], Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    kind = _compact_text(ref.get("kind"), max_chars=40) or "ref"
    ref_id = _compact_text(ref.get("id"), max_chars=160) or ""
    label = _compact_text(ref.get("label"), max_chars=180)
    descriptor: Dict[str, Any] = {
        "kind": kind,
        "id": ref_id,
        "label": label or f"{kind}:{ref_id}",
    }
    fallback_target = _fallback_ref_target(kind, ref_id)

    ref_identities = _ref_identity_values(kind, ref_id)
    matched = False
    for candidate in _context_package_descriptor_candidates(context_package):
        candidate_kind = candidate.get("kind")
        candidate_id = _compact_text(candidate.get("id"), max_chars=160)
        if candidate_kind != kind or not candidate_id:
            continue
        if ref_identities.isdisjoint(_ref_identity_values(kind, candidate_id)):
            continue

        if not matched:
            _merge_descriptor_fields(descriptor, candidate, overwrite_label=True)
        else:
            _merge_descriptor_fields(descriptor, candidate)
        matched = True
    resolved = (
        resolved_descriptors.get((kind, ref_id))
        if isinstance(resolved_descriptors, dict)
        else None
    )
    _merge_descriptor_fields(
        descriptor,
        resolved,
        overwrite_label=not matched,
    )
    if fallback_target and not descriptor.get("target"):
        normalized_target = normalize_target(fallback_target)
        if normalized_target:
            descriptor["target"] = normalized_target
    return descriptor


_PREVIEW_SIGNED_URL_MINUTES = 5

# Bound how many non-terminal compute jobs we surface live previews for, so a
# project with many concurrent runs never inflates the context package. These
# running-job items are prepended to the artifact items (see
# load_strategic_context_evidence) so live previews win the descriptor cut.
_RUNNING_JOB_PREVIEW_LIMIT = 4

# How many non-terminal compute-job rows to FETCH from the DB before filtering
# for viewable-bearing ones, so an older job with a manifest of mid-run plots
# is not hidden behind newer media-less rows.
_RUNNING_JOB_PREVIEW_FETCH = 20

# How many terminal-artifact rows to fetch from the DB before sorting for
# viewable-bearing ones and capping. Same goal: prevent newer non-viewable
# artifacts from burying an older artifact that carries actual image/video data.
_ARTIFACT_EVIDENCE_FETCH = 20

# How many terminal-artifact evidence items to surface in the context package
# (the final output cap — unchanged from the original implicit 4).
_ARTIFACT_EVIDENCE_LIMIT = 4


async def _validate_viewable_files(
    session: AsyncSession,
    project_id: Any,
    viewable_files: Any,
) -> List[tuple]:
    """Sequential ownership checks; returns (vf, classified) pairs that pass."""
    valid: List[tuple] = []
    if not isinstance(viewable_files, list):
        return valid
    for vf in viewable_files:
        if not isinstance(vf, dict):
            continue
        vf_storage_uri = vf.get("storage_uri")
        vf_kind = vf.get("kind", "image")
        vf_filename = vf.get("filename", "")
        if not vf_storage_uri or not isinstance(vf_storage_uri, str):
            continue
        # Defense in depth: only allowlisted-extension files whose stamped
        # kind matches the filename's classification are ever signed — a
        # manifest entry naming an svg (or misdeclaring its kind) is
        # skipped even if one was somehow persisted.
        classified = viewable_content_type_for_filename(vf_filename)
        if classified is None or classified[1] != vf_kind:
            continue
        try:
            await verify_artifact_project_ownership(
                vf_storage_uri, project_id, session
            )
        except Exception:  # noqa: BLE001 — ownership violation
            continue
        valid.append((vf, classified))
    return valid


def _build_preview_from_vf(vf: dict, signed_url: str) -> Dict[str, Any]:
    """Build a preview dict from a viewable_file entry and its signed URL."""
    preview: Dict[str, Any] = {
        "url": signed_url,
        "storage_uri": vf.get("storage_uri"),
        "kind": vf.get("kind", "image"),
        "filename": vf.get("filename", ""),
    }
    if isinstance(vf.get("revision"), str) and vf["revision"]:
        preview["revision"] = vf["revision"]
    if isinstance(vf.get("live"), bool):
        preview["live"] = vf["live"]
    return preview


async def _sign_viewable_manifest(
    session: AsyncSession,
    project_id: Any,
    viewable_files: Any,
) -> List[Dict[str, Any]]:
    """Mint short-lived signed preview URLs for a ``viewable_files`` manifest.

    Shared by the terminal-artifact path (``_resolve_viewable_previews``, reading
    an Artifact's ``logs.viewable_files``) and the running-compute-job path
    (``load_strategic_context_evidence``, reading a job's
    ``polling_metadata['viewable_files']``). Each entry is
    ``{filename, storage_uri, kind, ...}``; every one is independently verified
    for project ownership and re-validated against the viewable-extension
    allowlist (no svg — script execution via a signed URL), with a stamped
    ``kind`` that must match the filename's classification.

    Fail-soft: an ownership violation or signing failure skips that one file so
    the rest of the manifest still resolves. Returns the signed preview list
    (``[]`` when nothing resolves).

    Two-phase: DB ownership checks run sequentially (AsyncSession is not
    concurrency-safe), then all GCS signing coroutines are gathered in parallel.
    """
    # Phase 1: sequential ownership checks (session-dependent)
    valid_entries = await _validate_viewable_files(session, project_id, viewable_files)
    if not valid_entries:
        return []

    # Phase 2: parallel signing — all GCS round-trips in one gather
    sign_results = await asyncio.gather(
        *[
            generate_download_signed_url(
                vf.get("storage_uri"),
                expiration_minutes=_PREVIEW_SIGNED_URL_MINUTES,
            )
            for vf, _ in valid_entries
        ],
        return_exceptions=True,
    )

    return [
        _build_preview_from_vf(vf, result)
        for (vf, _), result in zip(valid_entries, sign_results, strict=False)
        if not isinstance(result, Exception)
    ]


async def _resolve_viewable_previews(
    session: AsyncSession,
    project_id,
    artifact: Artifact,
) -> List[Dict[str, Any]]:
    """Mint short-lived signed preview URLs for an evidence artifact's viewables.

    Two shapes resolve:

    * viewable files (images/videos) recorded in the artifact's ``logs``
      manifest (``viewable_files``, stamped at terminal compute-job linkage) —
      each entry is independently verified for project ownership and
      re-validated against the viewable-extension allowlist (no svg — script
      execution via a signed URL), with a stamped ``kind`` that must match the
      filename's classification;
    * the Artifact's own ``storage_uri`` when it is itself an allowlisted
      image/video object (e.g. a screenshot artifact).

    The signed URLs are minted HERE — at the async evidence-load boundary — and
    exist only in the in-flight context-package response. They are never
    persisted: the strategic-session snapshot seam
    (``workbench_state.sanitize_strategic_session_snapshot`` /
    ``_sanitize_ref_descriptor``) allowlists {kind,id,label,summary,status,
    target} and structurally drops image/video/preview payloads, and knowledge
    evidence rules (``knowledge_completion``) already reject bare http(s)
    citations — signed URLs stay transient helpers, never durable evidence.
    The raw ``gs://`` object reference DOES ride along on each preview as
    ``storage_uri`` so the frontend can lazily re-resolve a fresh signed URL
    when a preview's short-lived signature expires; it is scrubbed from the
    durable workbench projection at the snapshot seam
    (``_sanitize_ref_descriptor``), so it never becomes persisted evidence.

    Fail-soft everywhere: an ownership violation or signing failure skips that
    one file, and any parse error degrades to the metadata-only stub — a
    preview must never break the whole evidence package.
    """
    previews: List[Dict[str, Any]] = []

    # The Artifact's own object, when directly viewable. No per-file ownership
    # re-check needed: the artifact row itself came from the project-scoped
    # evidence query.
    direct = artifact.storage_uri if isinstance(artifact.storage_uri, str) else None
    direct_classified = (
        viewable_content_type_for_filename(direct) if direct else None
    )
    if direct and direct_classified is not None:
        _content_type, direct_kind = direct_classified
        try:
            url = await generate_download_signed_url(
                direct, expiration_minutes=_PREVIEW_SIGNED_URL_MINUTES
            )
        except Exception:  # noqa: BLE001 — fail-soft to the metadata-only stub
            url = None
        if url:
            previews.append(
                {
                    "url": url,
                    "storage_uri": direct,
                    "kind": direct_kind,
                    "filename": direct.rsplit("/", 1)[-1],
                }
            )

    if not artifact.logs:
        return previews
    try:
        logs_data = (
            json.loads(artifact.logs)
            if isinstance(artifact.logs, str)
            else artifact.logs
        )
        viewable_files = (
            logs_data.get("viewable_files", [])
            if isinstance(logs_data, dict)
            else []
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return previews
    previews.extend(
        await _sign_viewable_manifest(session, project_id, viewable_files)
    )
    return previews


_NON_TERMINAL_COMPUTE_JOB_STATUSES = (
    ComputeJobStatus.QUEUED,
    ComputeJobStatus.STARTING,
    ComputeJobStatus.RUNNING,
    ComputeJobStatus.POLLING,
)


async def _running_job_preview_items(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> List[Dict[str, Any]]:
    """Surface live viewable previews for RUNNING (non-terminal) compute jobs.

    The backend accumulates a job's mid-run viewable files into
    ``polling_metadata['viewable_files']`` (same manifest shape stamped at
    terminal linkage) WHILE the job runs. A running job has not linked a durable
    Artifact yet, so it would otherwise never appear as a renderable evidence
    artifact — the operator watching a multi-week RL run would see no live plots
    or rollout frames in the strategic canvas.

    We build synthetic artifact-shaped items carrying signed ``viewable_previews``
    (reusing the exact ownership + allowlist + signing pipeline as terminal
    artifacts via ``_sign_viewable_manifest``) so ``_artifact_descriptor``
    promotes the descriptor kind to image/video with a populated URL and no
    ``unsupported_reason`` stub. ``artifact_id IS NULL`` scopes this to jobs not
    yet linked to a durable artifact, so a terminal job is never double-surfaced.
    """
    rows = (
        await session.execute(
            select(ComputeJob, Task.title)
            .options(
                load_only(
                    ComputeJob.id,
                    ComputeJob.task_id,
                    ComputeJob.status,
                    ComputeJob.created_at,
                    ComputeJob.polling_metadata,
                    raiseload=True,
                )
            )
            .join(Task, ComputeJob.task_id == Task.id, isouter=True)
            .where(
                ComputeJob.project_id == project_id,
                ComputeJob.artifact_id.is_(None),
                ComputeJob.status.in_(_NON_TERMINAL_COMPUTE_JOB_STATUSES),
            )
            .order_by(ComputeJob.updated_at.desc())
            .limit(_RUNNING_JOB_PREVIEW_FETCH)
        )
    ).all()

    # Phase 1: sequential ownership checks across all jobs and files.
    # AsyncSession is not concurrency-safe, so DB calls remain sequential.
    job_meta: List[tuple] = []  # (job, task_title, pm, valid_entries)
    for job, task_title in rows:
        pm = job.polling_metadata if isinstance(job.polling_metadata, dict) else {}
        viewable_files = pm.get("viewable_files")
        if not isinstance(viewable_files, list) or not viewable_files:
            continue
        valid = await _validate_viewable_files(session, project_id, viewable_files)
        if valid:
            job_meta.append((job, task_title, pm, valid))

    if not job_meta:
        return []

    # Phase 2: gather ALL signing coroutines from ALL jobs in one batch.
    # generate_download_signed_url uses asyncio.to_thread and never touches
    # the session, so concurrent dispatch is safe.
    all_vf_flat = [
        (vf, classified)
        for _, _, _, valid in job_meta
        for vf, classified in valid
    ]
    sign_results = await asyncio.gather(
        *[
            generate_download_signed_url(
                vf.get("storage_uri"),
                expiration_minutes=_PREVIEW_SIGNED_URL_MINUTES,
            )
            for vf, _ in all_vf_flat
        ],
        return_exceptions=True,
    )

    # Phase 3: map results back to per-job previews and build items.
    items: List[Dict[str, Any]] = []
    offset = 0
    for job, task_title, pm, valid in job_meta:
        count = len(valid)
        job_sign_results = sign_results[offset : offset + count]
        offset += count

        previews = [
            _build_preview_from_vf(vf, result)
            for (vf, _), result in zip(valid, job_sign_results, strict=False)
            if not isinstance(result, Exception)
        ]
        if not previews:
            continue

        label = _compact_text(task_title, max_chars=100) or "Live compute previews"
        item = _with_target(
            {
                "id": str(job.id),
                "kind": "storage",
                "label": label,
                "artifact_type": "compute_result",
                "has_logs": True,
                "task_id": str(job.task_id) if job.task_id else None,
                "task_title": _compact_text(task_title, max_chars=120),
                "task_status": getattr(job.status, "value", job.status),
                "created_at": job.created_at.isoformat()
                if job.created_at
                else None,
                # Live signal so the canvas/panel can badge previews as still
                # accruing while the job runs.
                "compute_job_running": True,
                "viewable_previews": previews,
            },
            {
                "surface": "task",
                "task_id": str(job.task_id) if job.task_id else None,
            }
            if job.task_id
            else None,
        )
        # Stamp viewable_files_dropped from polling_metadata so the strategic
        # canvas can surface the omission count to the operator for running jobs.
        dropped = pm.get("viewable_files_dropped")
        if isinstance(dropped, int) and dropped > 0:
            item["viewable_files_dropped"] = dropped
        items.append(item)
    return items[:_RUNNING_JOB_PREVIEW_LIMIT]


async def load_strategic_context_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> Dict[str, Any]:
    """Return bounded durable context descriptors without heavy bodies/logs."""

    # Real aggregate count of all knowledge documents for the project, plus the
    # pending (unaddressed) subset — merged into one indexed round trip.
    _knowledge_count_row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(
                    KnowledgeDocument.lifecycle_status == "pending"
                ).label("pending"),
            ).where(KnowledgeDocument.project_id == project_id)
        )
    ).one()
    knowledge_total = int(_knowledge_count_row.total or 0)
    pending_count = int(_knowledge_count_row.pending or 0)

    # Age of the oldest unaddressed pending document so backlog health is
    # observable over time (no signal was tracked before).
    oldest_pending_created_at = (
        await session.execute(
            select(KnowledgeDocument.created_at)
            .where(
                KnowledgeDocument.project_id == project_id,
                KnowledgeDocument.lifecycle_status == "pending",
            )
            .order_by(KnowledgeDocument.created_at.asc())
            .limit(1)
        )
    ).scalar()
    oldest_pending_age_hours: float | None = None
    if oldest_pending_created_at is not None:
        delta = _utcnow() - oldest_pending_created_at
        oldest_pending_age_hours = round(delta.total_seconds() / 3600, 1)

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
                    KnowledgeDocument.lifecycle_status,
                    KnowledgeDocument.created_at,
                    KnowledgeDocument.updated_at,
                    raiseload=True,
                )
            )
            .where(
                KnowledgeDocument.project_id == project_id,
                KnowledgeDocument.lifecycle_status == "pending",
            )
            .order_by(
                KnowledgeDocument.updated_at.asc(),
                KnowledgeDocument.created_at.asc(),
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
                    Artifact.logs,
                    Artifact.created_at,
                    raiseload=True,
                )
            )
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Task.project_id == project_id,
                Task.archived.is_(False),
                meaningful_artifact_filter,
            )
            .order_by(Artifact.created_at.desc())
            .limit(_ARTIFACT_EVIDENCE_FETCH)
        )
    ).all()

    knowledge_items = []
    for doc in knowledge_rows:
        lc_status = doc.lifecycle_status or "pending"
        # Only surface pending (unconsumed, undismissed) docs in the evidence
        # section so the strategist sees actionable follow-ups, not already-
        # processed documents.
        if lc_status != "pending":
            continue
        knowledge_items.append(
            _with_target(
                {
                    "id": str(doc.id),
                    "kind": "knowledge_document",
                    "label": _compact_text(doc.title, max_chars=100),
                    "summary": _compact_text(doc.summary, max_chars=180),
                    "source_kind": _compact_text(doc.source_kind, max_chars=60),
                    "topic": _compact_text(doc.topic, max_chars=80),
                    "direction_id": _compact_text(doc.direction_id, max_chars=80),
                    "source_task_id": (
                        str(doc.source_task_id) if doc.source_task_id else None
                    ),
                    "lifecycle_status": lc_status,
                    "created_at": doc.created_at.isoformat()
                    if doc.created_at
                    else None,
                    "updated_at": doc.updated_at.isoformat()
                    if doc.updated_at
                    else None,
                },
                {
                    "surface": "knowledge",
                    "document_id": str(doc.id),
                    "direction_id": _compact_text(doc.direction_id, max_chars=80),
                    "task_id": str(doc.source_task_id) if doc.source_task_id else None,
                },
            )
        )
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
        item = _with_target(
            {
                "id": str(artifact.id),
                "kind": context_kind,
                "label": _compact_text(label, max_chars=100),
                "artifact_type": _compact_text(artifact_type, max_chars=80),
                "has_logs": bool(has_logs),
                "task_id": str(task_id) if task_id else None,
                "task_title": _compact_text(task_title, max_chars=120),
                "task_status": getattr(task_status, "value", task_status),
                "logical_key": _compact_text(artifact.logical_key, max_chars=160),
                "created_at": artifact.created_at.isoformat()
                if artifact.created_at
                else None,
            },
            {
                "surface": "task",
                "task_id": str(task_id) if task_id else None,
                "artifact_id": str(artifact.id),
            },
        )
        # Mint short-lived signed preview URLs for the artifact's viewable
        # files (bounded: at most the 4 evidence rows above). The signed URLs
        # are minted at THIS async load boundary and live only in the
        # in-flight response — see _resolve_viewable_previews for the
        # never-persist reasoning. Each preview also carries its raw
        # storage_uri so the frontend can lazily re-resolve an expired signed
        # URL; the snapshot seam scrubs it from the durable projection so it
        # never leaks into persisted evidence (test_strategic_state.py).
        viewable_previews = await _resolve_viewable_previews(
            session, project_id, artifact
        )
        if viewable_previews:
            item["viewable_previews"] = viewable_previews
        # Stamp viewable_files_dropped from the artifact's logs manifest so the
        # strategic canvas can surface the omission count to the operator (mirrors
        # how ComputeJobsPanel surfaces the same count in the task-detail drawer).
        if artifact.logs:
            try:
                logs_data = (
                    json.loads(artifact.logs)
                    if isinstance(artifact.logs, str)
                    else artifact.logs
                )
                if isinstance(logs_data, dict):
                    dropped = logs_data.get("viewable_files_dropped")
                    if isinstance(dropped, int) and dropped > 0:
                        item["viewable_files_dropped"] = dropped
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        artifact_items.append(item)

    # Stamp run provenance + earned evidence tier onto compute_experiment
    # artifacts from their backing compute knowledge document. This is the
    # canonical source (forward_quality_verdict.evidence_tier +
    # .terminal_diagnostics, written by knowledge_completion) — do NOT re-derive
    # the tier or execution location from raw job state. One bounded, batched
    # query keyed on the ≤4 artifact task ids already loaded above (no N+1).
    artifact_task_ids: List[uuid.UUID] = []
    for item in artifact_items:
        raw_task_id = item.get("task_id")
        if not raw_task_id:
            continue
        try:
            artifact_task_ids.append(uuid.UUID(str(raw_task_id)))
        except (ValueError, TypeError):
            continue
    if artifact_task_ids:
        compute_doc_rows = (
            await session.execute(
                select(
                    KnowledgeDocument.source_task_id,
                    KnowledgeDocument.forward_quality_verdict,
                )
                .where(
                    KnowledgeDocument.project_id == project_id,
                    KnowledgeDocument.source_kind
                    == SourceKind.COMPUTE_EXPERIMENT.value,
                    KnowledgeDocument.source_task_id.in_(artifact_task_ids),
                )
                .order_by(
                    KnowledgeDocument.updated_at.desc(),
                    KnowledgeDocument.created_at.desc(),
                )
            )
        ).all()
        provenance_by_task: Dict[str, Dict[str, Any]] = {}
        for source_task_id, verdict in compute_doc_rows:
            if not source_task_id or not isinstance(verdict, dict):
                continue
            task_key = str(source_task_id)
            # Newest doc per task wins (rows ordered updated_at desc above).
            if task_key in provenance_by_task:
                continue
            provenance: Dict[str, Any] = {}
            tier = verdict.get("evidence_tier")
            if isinstance(tier, str) and tier:
                provenance["evidence_tier"] = tier
            diagnostics = verdict.get("terminal_diagnostics")
            if isinstance(diagnostics, dict):
                for key in (
                    "executing_backend",
                    "runner_target_id",
                    "runner_hostname",
                ):
                    value = diagnostics.get(key)
                    if value:
                        provenance[key] = str(value)
            if provenance:
                provenance_by_task[task_key] = provenance
        for item in artifact_items:
            provenance = provenance_by_task.get(item.get("task_id") or "")
            if provenance:
                item.update(provenance)

    # Sort viewable-bearing terminal-artifact items first so the bounded
    # descriptor cut (_strategic_context_descriptors slices items[:4]) retains
    # an older artifact whose logs carry a viewable_files manifest even when
    # newer non-viewable rows filled the original SQL limit. Running-job items
    # are prepended below and win the first descriptor slots; this sort ensures
    # the remaining slots are filled by terminal artifacts WITH previews.
    artifact_items.sort(key=lambda i: 0 if i.get("viewable_previews") else 1)
    artifact_items = artifact_items[:_ARTIFACT_EVIDENCE_LIMIT]

    # Prepend live previews for RUNNING compute jobs so the operator watching a
    # long run sees plots/videos accrue in the canvas before the job terminates.
    # Prepending puts them ahead of terminal artifacts in the bounded descriptor
    # cut (_strategic_context_descriptors slices items[:4]); artifact_total stays
    # the durable-artifact count and is unaffected by these transient items.
    running_items = await _running_job_preview_items(session, project_id)
    if running_items:
        artifact_items = running_items + artifact_items

    return {
        "knowledge": {
            "total": int(knowledge_total or 0),
            "pending_count": int(pending_count or 0),
            "oldest_pending_age_hours": oldest_pending_age_hours,
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


def _descriptor_detail(
    label: Any,
    value: Any,
    *,
    target: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    compact_label = _compact_text(label, max_chars=80)
    if not compact_label:
        return None
    compact_value = _compact_text(value, max_chars=220)
    if compact_value is None and value is not None:
        compact_value = str(value)
    if compact_value is None:
        return None
    detail: Dict[str, Any] = {"label": compact_label, "value": compact_value}
    if isinstance(target, dict):
        detail["target"] = target
    return detail


def _strategic_context_source_table_descriptor(
    sources: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    rows = [
        {
            "id": _compact_text(source.get("kind"), max_chars=80),
            "cells": {
                "source": str(source.get("kind") or "source").replace("_", " "),
                "count": int(source.get("count") or 0),
            },
        }
        for source in sources[:8]
    ]
    if not rows:
        return None
    return {
        "schema_version": 1,
        "id": "sources",
        "kind": "table",
        "title": "Context sources",
        "summary": "Bounded source counts included in this package.",
        "status": "ready",
        "columns": [
            {"key": "source", "label": "Source"},
            {"key": "count", "label": "Items"},
        ],
        "rows": rows,
    }


def _artifact_renderable_kind(artifact: Dict[str, Any]) -> str:
    fingerprint = " ".join(
        str(value or "").lower()
        for value in [
            artifact.get("artifact_type"),
            artifact.get("logical_key"),
            artifact.get("label"),
            artifact.get("kind"),
        ]
    )
    if any(
        token in fingerprint
        for token in ["image", ".png", ".jpg", ".jpeg", ".webp", ".gif"]
    ):
        return "image"
    if any(
        token in fingerprint
        for token in ["video", ".mp4", ".webm", ".mov"]
    ):
        return "video"
    if any(token in fingerprint for token in ["pdf", ".pdf"]):
        return "pdf"
    if any(token in fingerprint for token in ["chart", "plot", "graph"]):
        return "chart"
    if any(
        token in fingerprint
        for token in ["markdown", "report", "summary", "note", "design"]
    ):
        return "markdown"
    if any(token in fingerprint for token in ["log", "trace", "stdout", "stderr"]):
        return "text"
    return "resource_detail"


def _artifact_descriptor(artifact: Dict[str, Any]) -> Dict[str, Any] | None:
    artifact_id = _compact_text(artifact.get("id"), max_chars=120)
    if not artifact_id:
        return None
    # Resolved previews (signed URLs minted at evidence-load time — see
    # _resolve_viewable_previews) decide the renderable kind directly, so a
    # compute-result bundle whose manifest recorded a plot/video renders it
    # inline; without any, fall back to the fingerprint heuristic (old
    # behavior, incl. the image stub for image-looking artifacts with no
    # signable object).
    raw_previews = artifact.get("viewable_previews")
    previews = [
        p
        for p in (raw_previews if isinstance(raw_previews, list) else [])
        if isinstance(p, dict) and (p.get("url") or p.get("storage_uri"))
    ]
    image_url = next((p.get("url") for p in previews if p.get("kind") == "image"), None)
    video_url = next((p.get("url") for p in previews if p.get("kind") == "video"), None)
    pdf_url = next((p.get("url") for p in previews if p.get("kind") == "pdf"), None)
    if image_url:
        kind = "image"
    elif video_url:
        kind = "video"
    elif pdf_url:
        kind = "pdf"
    else:
        kind = _artifact_renderable_kind(artifact)
    title = (
        _compact_text(artifact.get("label"), max_chars=90)
        or _compact_text(artifact.get("artifact_type"), max_chars=90)
        or "Artifact"
    )
    # Run provenance for compute_experiment artifacts (stamped onto the item in
    # load_strategic_context_evidence from the backing compute knowledge doc).
    # Renders generically via the shared _descriptor_detail helper.
    runner_hostname = artifact.get("runner_hostname")
    runner_target_id = artifact.get("runner_target_id")
    if runner_hostname and runner_target_id:
        runner_label: Any = f"{runner_hostname} ({runner_target_id})"
    else:
        runner_label = runner_hostname or runner_target_id
    details = [
        _descriptor_detail("Task", artifact.get("task_title"), target=artifact.get("target")),
        _descriptor_detail("Type", artifact.get("artifact_type") or artifact.get("kind")),
        _descriptor_detail("Status", artifact.get("task_status")),
        _descriptor_detail("Executed on", artifact.get("executing_backend")),
        _descriptor_detail("Runner", runner_label),
        _descriptor_detail("Created", artifact.get("created_at")),
    ]
    descriptor: Dict[str, Any] = {
        "schema_version": 1,
        "id": f"artifact:{artifact_id}",
        "kind": kind,
        "title": title,
        "summary": _compact_text(artifact.get("task_title"), max_chars=160),
        "status": _compact_text(artifact.get("task_status"), max_chars=60)
        or "ready",
        "target": artifact.get("target"),
        "details": [detail for detail in details if detail],
    }
    # Earned-vs-claimed evidence tier (canonical value from the compute doc's
    # forward_quality_verdict.evidence_tier). Additive/backward-compatible — the
    # frontend maps it to the shared EvidenceTierBadge; absent for non-compute
    # artifacts.
    evidence_tier = _compact_text(artifact.get("evidence_tier"), max_chars=60)
    if evidence_tier:
        descriptor["evidence_tier"] = evidence_tier
    # viewable_files_dropped from the compute-job viewable-files persistence
    # pipeline (capped at 25 files / 8 MiB each). Additive/backward-compatible
    # — the frontend surfaces the count as an amber "N file(s) omitted" note;
    # absent/0/None for descriptors without dropped files.
    viewable_files_dropped = artifact.get("viewable_files_dropped")
    if isinstance(viewable_files_dropped, int) and viewable_files_dropped > 0:
        descriptor["viewable_files_dropped"] = viewable_files_dropped
    image_previews = [p for p in previews if p.get("kind") == "image"]
    video_previews = [p for p in previews if p.get("kind") == "video"]
    pdf_previews = [p for p in previews if p.get("kind") == "pdf"]

    def add_preview_group(
        field: str, grouped_previews: list[Dict[str, Any]]
    ) -> bool:
        if not grouped_previews:
            return False
        primary = grouped_previews[0]
        descriptor[field] = {
            "url": primary.get("url"),
            "alt": title,
            "storage_uri": primary.get("storage_uri"),
            **({"revision": primary.get("revision")} if "revision" in primary else {}),
            **({"live": primary.get("live")} if "live" in primary else {}),
        }
        descriptor[f"{field}s"] = [
            {
                "url": preview.get("url"),
                "alt": preview.get("filename") or title,
                "filename": preview.get("filename"),
                "storage_uri": preview.get("storage_uri"),
                **(
                    {"revision": preview.get("revision")}
                    if "revision" in preview
                    else {}
                ),
                **({"live": preview.get("live")} if "live" in preview else {}),
            }
            for preview in grouped_previews
        ]
        return True

    # Retain every preview type on a mixed-media artifact. ``kind`` remains a
    # primary tile classification for older consumers, but must not discard a
    # rollout video or PDF merely because an image has precedence.
    has_images = add_preview_group("image", image_previews)
    has_videos = add_preview_group("video", video_previews)
    has_pdfs = add_preview_group("pdf", pdf_previews)

    if kind == "markdown":
        descriptor["body"] = (
            descriptor["summary"] or "Read-only artifact metadata is available."
        )
        descriptor["language"] = "markdown"
    elif kind == "text":
        descriptor["body"] = (
            descriptor["summary"] or "Read-only log metadata is available."
        )
        descriptor["language"] = "text"
    elif kind == "image" and not has_images:
        # OUR filter wins: ``previews`` (top of function) is already restricted
        # to signed-URL previews (svg/kind-mismatch dropped by
        # _resolve_viewable_previews). Adopt engine's multi-image gallery list,
        # built from that filtered set — emits url/alt/filename only, never a
        # raw storage_uri.
        if not add_preview_group("image", previews):
            descriptor["image"] = {"url": None, "alt": title, "storage_uri": None}
            descriptor["images"] = []
            descriptor[
                "unsupported_reason"
            ] = "Image artifact previews require a signed preview URL."
    elif kind == "video" and not has_videos:
        # OUR filtered ``previews`` (signed-URL-only) + engine's multi-video
        # gallery list; url/alt/filename only, never a raw storage_uri.
        if not add_preview_group("video", previews):
            descriptor["video"] = {"url": None, "alt": title, "storage_uri": None}
            descriptor["videos"] = []
            descriptor[
                "unsupported_reason"
            ] = "Video artifact previews require a signed preview URL."
    elif kind == "pdf" and not has_pdfs:
        # OUR filtered ``previews`` (signed-URL-only) + a parallel gallery list
        # for multiple pdfs. A PDF is opened as a top-level new-tab navigation
        # (never an inline app-origin iframe), so it renders in the browser's
        # sandboxed PDF viewer on the storage origin. url/alt/filename only,
        # never a raw storage_uri in the gallery entries.
        if not add_preview_group("pdf", previews):
            descriptor["pdf"] = {"url": None, "alt": title, "storage_uri": None}
            descriptor["pdfs"] = []
            descriptor[
                "unsupported_reason"
            ] = "PDF artifact previews require a signed preview URL."
    elif kind == "chart":
        descriptor["chart"] = {
            "kind": "bar",
            "points": [{"label": "artifact", "value": 1}],
        }
    return descriptor


def _resource_descriptor(resource: Dict[str, Any]) -> Dict[str, Any] | None:
    resource_id = _compact_text(resource.get("id"), max_chars=140)
    if not resource_id:
        return None
    details = [
        _descriptor_detail("Kind", resource.get("kind")),
        _descriptor_detail("Summary", resource.get("summary"), target=resource.get("target")),
        _descriptor_detail("Updated", resource.get("updated_at") or resource.get("created_at")),
    ]
    return {
        "schema_version": 1,
        "id": resource_id,
        "kind": "resource_detail",
        "title": _compact_text(resource.get("label"), max_chars=90) or resource_id,
        "summary": _compact_text(resource.get("summary"), max_chars=180),
        "status": "ready",
        "target": resource.get("target"),
        "details": [detail for detail in details if detail],
    }


def _strategic_context_descriptors(
    *,
    canvas: Dict[str, Any],
    resources: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    evidence: Dict[str, Any],
    source_item_count: int,
    task_counts: Dict[str, int],
    attention_count: int,
    status: str,
) -> List[Dict[str, Any]]:
    blocked_count = int(task_counts.get("blocked") or 0)
    failed_count = int(task_counts.get("failed") or 0)
    descriptors: List[Dict[str, Any]] = [
        {
            "schema_version": 1,
            "id": canvas["id"],
            "kind": "context_map",
            "title": canvas["title"],
            "summary": f"{canvas['node_count']} nodes / {canvas['edge_count']} links",
            "status": status,
            "target": {"surface": "project", "section": "problem-model"},
            "empty": int(canvas.get("node_count") or 0) == 0,
        },
        {
            "schema_version": 1,
            "id": "context-metrics",
            "kind": "metric_cards",
            "title": "Context health",
            "summary": "Read-only counts from the Strategic package.",
            "status": status,
            "metrics": [
                {
                    "label": "Items",
                    "value": source_item_count,
                    "tone": "info",
                    "target": {"surface": "project", "section": "problem-model"},
                },
                {
                    "label": "Tasks",
                    "value": int(task_counts.get("total") or 0),
                    "tone": "neutral",
                    "target": {"surface": "pipeline"},
                },
                {
                    "label": "Active",
                    "value": int(
                        task_counts.get("active") or task_counts.get("running") or 0
                    ),
                    "tone": "active",
                    "target": {"surface": "pipeline", "section": "flow"},
                },
                {
                    "label": "Attention",
                    "value": attention_count,
                    "tone": "warning" if attention_count else "neutral",
                    "target": {"surface": "runtime", "section": "health"},
                },
                {
                    "label": "Blocked",
                    "value": blocked_count,
                    "tone": "critical" if blocked_count else "neutral",
                    "target": {"surface": "pipeline", "section": "flow"},
                },
                {
                    "label": "Failed",
                    "value": failed_count,
                    "tone": "critical" if failed_count else "neutral",
                    "target": {"surface": "pipeline", "section": "flow"},
                },
            ],
        },
    ]
    source_table = _strategic_context_source_table_descriptor(sources)
    if source_table:
        descriptors.append(source_table)

    seen_ids = {str(descriptor["id"]) for descriptor in descriptors}
    for artifact in (evidence.get("artifacts") or {}).get("items", [])[:4]:
        descriptor = _artifact_descriptor(artifact)
        if descriptor and descriptor["id"] not in seen_ids:
            seen_ids.add(str(descriptor["id"]))
            descriptors.append(descriptor)

    for resource in resources[:5]:
        if resource.get("kind") == "canvas":
            continue
        descriptor = _resource_descriptor(resource)
        if descriptor and descriptor["id"] not in seen_ids:
            seen_ids.add(str(descriptor["id"]))
            descriptors.append(descriptor)

    if len(descriptors) == 2 and source_table is None:
        descriptors.append(
            {
                "schema_version": 1,
                "id": "unsupported-empty-artifacts",
                "kind": "unsupported",
                "title": "No artifacts attached",
                "summary": "The context package is ready, but no renderable artifacts are available.",
                "status": "empty",
                "empty": True,
            }
        )
    return descriptors[:10]


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
        _drilldown(
            "running_update",
            "Update",
            {"surface": "project", "section": "running-update"},
        ),
        _drilldown(
            "problem_model",
            "Model",
            {"surface": "project", "section": "problem-model"},
        ),
        _drilldown("directions", "Directions", {"surface": "project", "section": "directions"}),
        _drilldown("pipeline", "Pipeline", {"surface": "pipeline"}),
        _drilldown("runtime", "Runtime", {"surface": "runtime", "section": "health"}),
    ]
    if project.design_doc:
        drilldowns.append(
            _drilldown("design", "Design", {"surface": "project", "section": "design-doc"})
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
        _with_target(
            {
                "id": f"knowledge:{doc.get('id')}",
                "kind": "knowledge_document",
                "label": doc.get("label") or "Knowledge document",
                "summary": doc.get("summary"),
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
            },
            doc.get("target"),
        )
        for doc in (evidence.get("knowledge") or {}).get("items", [])[:3]
    ]
    artifact_resources = [
        _with_target(
            {
                "id": f"artifact:{artifact.get('id')}",
                "kind": "artifact",
                "label": artifact.get("label")
                or artifact.get("artifact_type")
                or "Artifact",
                "summary": artifact.get("task_title"),
                "created_at": artifact.get("created_at"),
            },
            artifact.get("target"),
        )
        for artifact in (evidence.get("artifacts") or {}).get("items", [])[:3]
    ]
    mixed_resources: list[Dict[str, Any]] = []
    for index in range(max(len(knowledge_resources), len(artifact_resources))):
        if index < len(knowledge_resources):
            mixed_resources.append(knowledge_resources[index])
        if index < len(artifact_resources):
            mixed_resources.append(artifact_resources[index])
    resources = [
        _with_target(
            {
                "id": canvas["id"],
                "kind": "canvas",
                "label": canvas["title"],
                "summary": f"{canvas['node_count']} nodes / {canvas['edge_count']} links",
            },
            {"surface": "project", "section": "problem-model"},
        ),
        *mixed_resources,
    ][:7]
    descriptors = _strategic_context_descriptors(
        canvas=canvas,
        resources=resources,
        sources=sources,
        evidence=evidence,
        source_item_count=source_item_count,
        task_counts=task_counts,
        attention_count=attention_count,
        status=status,
    )

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
        "descriptors": descriptors,
    }
