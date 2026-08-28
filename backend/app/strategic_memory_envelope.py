"""Durable, byte-identical Strategic context-envelope snapshots.

This module is the sole serializer for the core context sent to Fast and
Workday.  Consumers receive ``StrategicEnvelopeTransport`` and must decode the
stored bytes rather than re-serializing the core payload themselves.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Project,
    ProjectMessage,
    StrategicContextEnvelopeSnapshot,
    StrategicMemoryProjectState,
    _purge_derived_strategic_context_for_privacy_mutation,
)
from app.strategic_conversation_query import strategic_conversation_predicate
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)


# Phase 2 snapshots are a short-lived derived prompt cache, not the durable
# cross-session memory authority.  A cold Resident reconstructs from canonical
# transcript rows plus project/problem-memory state; it must never depend on a
# historical snapshot surviving this ten-minute cache window.
DEFAULT_ENVELOPE_TTL = timedelta(minutes=10)
# A private Workday hand-off may outlive the foreground's short response
# window.  Its pinned core remains a bounded derived cache, never an archive.
MAX_PACKET_PINNED_ENVELOPE_TTL = timedelta(days=7)
DEFAULT_TRANSCRIPT_SUFFIX_LIMIT = 32
# V2 adds fenced retrieval evidence/raw suffixes to the persisted shared core.
ENVELOPE_SCHEMA_VERSION = 2
# The foreground prompt is deliberately much smaller than the provider context
# window. An envelope is a responsive conversational affordance, not a second
# archive of every opaque ProjectMessage context blob.
MAX_CANONICAL_ENVELOPE_BYTES = 64 * 1024
MAX_PROBLEM_MODEL_BYTES = 20 * 1024
MAX_DECISION_AUTHORITY_BYTES = 8 * 1024
MAX_MEMORY_ITEMS = 16
MAX_MEMORY_NOTE_BYTES = 768
MAX_EVIDENCE_CARD_TEXT_BYTES = 1_024
MAX_EVIDENCE_QUOTE_BYTES = 1_024
MAX_RAW_SUFFIX_ROWS = 6
MAX_TRANSCRIPT_CONTENT_BYTES = 2 * 1024
MAX_TRANSCRIPT_CONTEXT_BYTES = 1024
MAX_TRANSCRIPT_ROWS = 32
MAX_TRANSCRIPT_ATTACHMENTS = 24
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991

# Only these non-content fields may be copied into a durable Workday packet.
# The canonical bytes themselves remain in ``StrategicContextEnvelopeSnapshot``
# and are resolved again behind the current authority/privacy fence when a
# worker receives the packet.  Keeping a second base64 copy in ProjectMessage
# would make source redaction unable to erase all derived prompt material.
ENVELOPE_REFERENCE_FIELDS = (
    "snapshot_id",
    "canonical_sha256",
    "context_authority_generation",
    "privacy_generation",
    "transcript_sequence",
    "ledger_revision",
    "expires_at",
)

# These fields communicate conversational provenance without copying arbitrary
# message metadata (attachments, internal routing, credentials, or provider
# payloads) into another durable content-bearing record.
_TRANSCRIPT_CONTEXT_ALLOWLIST = frozenset({
    "event_type", "message_role", "source", "topic",
})
_MEMORY_ITEM_ALLOWLIST = frozenset({"id", "note", "set_at", "topic"})


class ContextEnvelopeFenceError(RuntimeError):
    """Raised when a caller tries to create a snapshot from stale authority."""


class StrategicEnvelopeTransport(BaseModel):
    """Wire DTO shared by Fast and Workday.

    ``canonical_envelope_b64`` is the exact ``LargeBinary`` value persisted on
    the snapshot.  It is intentionally not a JSON object.
    """

    snapshot_id: uuid.UUID
    canonical_envelope_b64: str
    canonical_sha256: str
    context_authority_generation: int
    privacy_generation: int
    transcript_sequence: int
    ledger_revision: int
    expires_at: datetime


@dataclass(frozen=True)
class CanonicalEnvelopeCore:
    """The immutable core passed to the persistence boundary."""

    canonical_envelope: bytes
    canonical_sha256: str


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    raise TypeError(f"not canonical JSON: {type(value).__name__}")


def canonical_envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    """Return one deterministic UTF-8 representation of an envelope mapping."""

    if not isinstance(envelope, Mapping):
        raise TypeError("canonical envelope must be a mapping")
    return json.dumps(
        dict(envelope),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def canonical_sha256(canonical_envelope: bytes) -> str:
    return hashlib.sha256(canonical_envelope).hexdigest()


def _canonical_gcs_locator(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    bucket = ""
    object_name = ""
    if parsed.scheme == "gs" and parsed.netloc:
        bucket = parsed.netloc.lower()
        object_name = parsed.path.lstrip("/")
    elif parsed.scheme == "https" and parsed.hostname:
        hostname = parsed.hostname.lower()
        if parsed.username or parsed.password or parsed.port is not None:
            return None
        if hostname == "storage.googleapis.com":
            bucket, separator, object_name = parsed.path.lstrip("/").partition("/")
            if not separator:
                return None
            bucket = bucket.lower()
        elif hostname.endswith(".storage.googleapis.com"):
            bucket = hostname[: -len(".storage.googleapis.com")]
            object_name = parsed.path.lstrip("/")
    if not bucket or not object_name:
        return None
    return f"gs://{bucket}/{object_name}"


def _canonical_base64_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) % 4:
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    return decoded if base64.b64encode(decoded).decode("ascii") == value else None


def canonical_attachment_manifest(
    attachments: object,
) -> list[dict[str, Any]]:
    """Bind model-visible attachments without persisting credentials or bytes."""

    if attachments is None:
        return []
    if not isinstance(attachments, Sequence) or isinstance(attachments, (str, bytes)):
        raise ValueError("canonical attachments must be a sequence")
    if len(attachments) > MAX_TRANSCRIPT_ATTACHMENTS:
        raise ValueError("canonical attachment manifest exceeds item limit")

    manifest: list[dict[str, Any]] = []
    for position, attachment in enumerate(attachments):
        if not isinstance(attachment, Mapping):
            raise ValueError("canonical attachment entries must be mappings")
        entry: dict[str, Any] = {"position": position}

        identity_key = "position"
        identity_value = str(position)
        for candidate in ("artifact_ref_id", "attachment_id", "id"):
            raw_identity = attachment.get(candidate)
            if raw_identity is not None and str(raw_identity).strip():
                identity_key = candidate
                identity_value = str(raw_identity).strip()
                break
        entry["identity_sha256"] = canonical_sha256(
            f"{identity_key}:{identity_value}".encode("utf-8")
        )

        attachment_type = str(
            attachment.get("type") or attachment.get("kind") or "attachment"
        ).strip().lower()
        entry["type"] = attachment_type or "attachment"
        mime_type = str(
            attachment.get("mime_type") or attachment.get("media_type") or ""
        ).strip().lower()
        if mime_type:
            entry["mime_type"] = mime_type
        name = str(attachment.get("name") or attachment.get("filename") or "").strip()
        if name:
            entry["name_sha256"] = canonical_sha256(name.encode("utf-8"))

        decoded_data = _canonical_base64_bytes(attachment.get("data"))
        raw_size = attachment.get("size_bytes")
        if raw_size is not None:
            if (
                isinstance(raw_size, bool)
                or not isinstance(raw_size, int)
                or raw_size < 0
                or raw_size > MAX_SAFE_JSON_INTEGER
            ):
                raise ValueError("canonical attachment size must be a nonnegative integer")
            entry["size_bytes"] = raw_size
        elif decoded_data is not None:
            entry["size_bytes"] = len(decoded_data)

        supplied_content_sha = str(
            attachment.get("image_checksum") or attachment.get("sha256") or ""
        ).strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", supplied_content_sha):
            entry["content_sha256"] = supplied_content_sha
        elif decoded_data is not None:
            entry["content_sha256"] = canonical_sha256(decoded_data)

        revision_checksum = str(attachment.get("checksum") or "").strip().lower()
        if revision_checksum:
            if re.fullmatch(r"[0-9a-f]{64}", revision_checksum):
                entry["revision_checksum"] = revision_checksum
            else:
                entry["revision_checksum_sha256"] = canonical_sha256(
                    revision_checksum.encode("utf-8")
                )

        locator = _canonical_gcs_locator(
            attachment.get("storage_uri") or attachment.get("url")
        )
        if locator:
            entry["locator_sha256"] = canonical_sha256(locator.encode("utf-8"))
        if attachment.get("required") is True:
            entry["required"] = True
        if isinstance(attachment.get("truncated"), bool):
            entry["truncated"] = attachment["truncated"]
        for count_field in ("snapshot_char_count", "original_char_count"):
            count_value = attachment.get(count_field)
            if count_value is None:
                continue
            if (
                isinstance(count_value, bool)
                or not isinstance(count_value, int)
                or count_value < 0
                or count_value > MAX_SAFE_JSON_INTEGER
            ):
                raise ValueError(f"canonical attachment {count_field} is invalid")
            entry[count_field] = count_value

        entry["descriptor_sha256"] = canonical_sha256(canonical_envelope_bytes(entry))
        manifest.append(entry)
    return manifest


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    """Keep a valid UTF-8 prefix without splitting a codepoint."""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", "ignore"), True


def _bounded_scalar(value: object, *, max_bytes: int) -> object | None:
    """Return a JSON scalar suitable for a low-trust context projection."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _truncate_utf8(value, max_bytes)[0]
    return None


def _bounded_mapping(
    value: Mapping[str, Any] | None,
    *,
    max_bytes: int,
    label: str,
) -> dict[str, Any]:
    """Keep a complete mapping when small, otherwise expose a safe receipt."""

    raw = dict(value or {})
    try:
        encoded = canonical_envelope_bytes(raw)
    except (TypeError, ValueError):
        return {"_unavailable": label, "_reason": "non_canonical_value"}
    if len(encoded) <= max_bytes:
        return raw
    preview, _ = _truncate_utf8(encoded.decode("utf-8"), max_bytes)
    return {
        "_truncated": True,
        "_kind": label,
        "_sha256": canonical_sha256(encoded),
        "_bytes": len(encoded),
        "_preview": preview,
    }


def _message_value(message: ProjectMessage | Mapping[str, Any], name: str) -> Any:
    return message.get(name) if isinstance(message, Mapping) else getattr(message, name)


def canonical_transcript_suffix(
    messages: Sequence[ProjectMessage | Mapping[str, Any]],
    *,
    attachment_manifests: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Project canonical conversation rows into stable, complete core records.

    The sequence is the authoritative ordering cursor.  Context is retained so
    decision/provenance evidence is not silently lost across a cold restart;
    only JSON-compatible values may enter this durable transcript contract.
    """

    normalized: list[dict[str, Any]] = []
    for message in messages[-MAX_TRANSCRIPT_ROWS:]:
        sequence = _message_value(message, "sequence")
        if sequence is None or int(sequence) < 0:
            raise ValueError("canonical transcript rows require a nonnegative sequence")
        message_id = _message_value(message, "id")
        if message_id is None:
            raise ValueError("canonical transcript rows require an id")
        context = _message_value(message, "context") or {}
        if not isinstance(context, Mapping):
            raise ValueError("canonical transcript context must be a mapping")
        projected_context: dict[str, Any] = {}
        for key in sorted(_TRANSCRIPT_CONTEXT_ALLOWLIST):
            if key not in context:
                continue
            scalar = _bounded_scalar(
                context[key], max_bytes=MAX_TRANSCRIPT_CONTEXT_BYTES
            )
            if scalar is not None:
                projected_context[key] = scalar
        encoded_context = canonical_envelope_bytes(projected_context)
        if len(encoded_context) > MAX_TRANSCRIPT_CONTEXT_BYTES:
            projected_context = {
                "_truncated": True,
                "_sha256": canonical_sha256(encoded_context),
                "_bytes": len(encoded_context),
            }
        content = str(_message_value(message, "content") or "")
        content_preview, content_truncated = _truncate_utf8(
            content, MAX_TRANSCRIPT_CONTENT_BYTES
        )
        row = {
            "sequence": int(sequence),
            "message_id": str(message_id),
            "from_type": str(_message_value(message, "from_type") or ""),
            "content": content_preview,
            "content_sha256": canonical_sha256(content.encode("utf-8")),
            "content_bytes": len(content.encode("utf-8")),
            "content_truncated": content_truncated,
            "context": projected_context,
        }
        if attachment_manifests is not None and str(message_id) in attachment_manifests:
            row["attachment_manifest"] = [
                dict(item) for item in attachment_manifests[str(message_id)]
            ]
        normalized.append(row)
    return sorted(normalized, key=lambda row: (row["sequence"], row["message_id"]))


def canonical_memory_projection(
    memory: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Bound legacy memory notes without allowing them to dominate Fast."""

    projected: list[dict[str, Any]] = []
    for item in list(memory or [])[-MAX_MEMORY_ITEMS:]:
        if not isinstance(item, Mapping):
            continue
        result: dict[str, Any] = {}
        for key in sorted(_MEMORY_ITEM_ALLOWLIST):
            if key not in item:
                continue
            scalar = _bounded_scalar(item[key], max_bytes=MAX_MEMORY_NOTE_BYTES)
            if scalar is not None:
                result[key] = scalar
        note = item.get("note")
        if isinstance(note, str):
            note_preview, note_truncated = _truncate_utf8(note, MAX_MEMORY_NOTE_BYTES)
            result["note"] = note_preview
            result["note_sha256"] = canonical_sha256(note.encode("utf-8"))
            result["note_truncated"] = note_truncated
        if result:
            projected.append(result)
    return projected


def canonical_retrieval_projection(
    retrieval: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bound V2 retrieval output for the shared persisted Fast/Workday core.

    The retrieval layer has already enforced scope and source availability.  We
    nevertheless serialize only its small evidence contract here so arbitrary
    trace metadata or a future provider response cannot become prompt memory.
    """

    value = dict(retrieval or {})
    cards: list[dict[str, Any]] = []
    for card in value.get("evidence_cards", ())[:MAX_MEMORY_ITEMS]:
        if not isinstance(card, Mapping):
            continue
        projected: dict[str, Any] = {}
        for key in (
            "item_id", "version", "status", "epistemic_status", "confidence",
            "recorded_source_sequence", "asserted_effective_from",
            "asserted_effective_until", "raw_quote_available",
        ):
            raw_value = card.get(key)
            scalar = (
                raw_value.isoformat()
                if isinstance(raw_value, datetime)
                else _bounded_scalar(raw_value, max_bytes=256)
            )
            if scalar is not None:
                projected[key] = scalar
        for key, limit in (("canonical_text", MAX_EVIDENCE_CARD_TEXT_BYTES), ("raw_quote", MAX_EVIDENCE_QUOTE_BYTES)):
            if isinstance(card.get(key), str):
                projected[key], projected[f"{key}_truncated"] = _truncate_utf8(card[key], limit)
        source = card.get("source")
        if isinstance(source, Mapping):
            projected["source"] = {
                key: _bounded_scalar(source.get(key), max_bytes=256)
                for key in ("id", "type", "key", "revision")
                if _bounded_scalar(source.get(key), max_bytes=256) is not None
            }
        for key in ("relation_path", "selection_reasons"):
            if isinstance(card.get(key), Sequence) and not isinstance(card.get(key), (str, bytes)):
                projected[key] = [
                    _truncate_utf8(str(entry), 256)[0] for entry in card[key][:8]
                ]
        if projected:
            cards.append(projected)
    suffix: list[dict[str, Any]] = []
    for source in value.get("raw_suffix", ())[:MAX_RAW_SUFFIX_ROWS]:
        if not isinstance(source, Mapping):
            continue
        projected = {}
        for key in ("source_id", "source_sequence", "source_type", "source_key", "content_hash", "content_truncated"):
            scalar = _bounded_scalar(source.get(key), max_bytes=256)
            if scalar is not None:
                projected[key] = scalar
        if isinstance(source.get("content"), str):
            projected["content"], was_truncated = _truncate_utf8(
                source["content"], MAX_TRANSCRIPT_CONTENT_BYTES
            )
            projected["content_truncated"] = bool(source.get("content_truncated")) or was_truncated
        if projected:
            suffix.append(projected)
    answerability = value.get("answerability")
    return {
        "evidence_cards": cards,
        "raw_suffix": suffix,
        "answerability": _bounded_mapping(
            answerability if isinstance(answerability, Mapping) else {},
            max_bytes=512,
            label="answerability",
        ),
        "memory_degraded": bool(value.get("memory_degraded", False)),
        "retrieval_trace": _bounded_mapping(
            value.get("retrieval_trace") if isinstance(value.get("retrieval_trace"), Mapping) else {},
            max_bytes=2_048,
            label="retrieval_trace",
        ),
    }


def _envelope_budget_receipt(value: object, label: str) -> dict[str, Any]:
    """Replace an over-budget component with a verifiable non-content receipt."""

    try:
        encoded = canonical_envelope_bytes(
            value if isinstance(value, Mapping) else {"items": value}
        )
    except (TypeError, ValueError):
        return {"_unavailable": label, "_reason": "non_canonical_value"}
    return {
        "_truncated": True,
        "_kind": label,
        "_sha256": canonical_sha256(encoded),
        "_bytes": len(encoded),
    }


def build_canonical_envelope_core(
    *,
    project_id: uuid.UUID,
    context_authority_generation: int,
    privacy_generation: int,
    transcript_sequence: int,
    ledger_revision: int,
    problem_model: Mapping[str, Any] | None,
    decision_authority: Mapping[str, Any] | None,
    memory: Sequence[Mapping[str, Any]] | None,
    transcript_suffix: Sequence[ProjectMessage | Mapping[str, Any]],
    attachment_manifests: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    retrieval: Mapping[str, Any] | None = None,
) -> CanonicalEnvelopeCore:
    """Build the one persisted envelope core; never serialize it per consumer."""

    problem = _bounded_mapping(
        problem_model, max_bytes=MAX_PROBLEM_MODEL_BYTES, label="problem_model"
    )
    decisions = _bounded_mapping(
        decision_authority,
        max_bytes=MAX_DECISION_AUTHORITY_BYTES,
        label="decision_authority",
    )
    memory_projection = canonical_memory_projection(memory)
    retrieval_projection = canonical_retrieval_projection(retrieval)
    transcript_rows = canonical_transcript_suffix(
        transcript_suffix,
        attachment_manifests=attachment_manifests,
    )

    def _assemble(
        rows: list[dict[str, Any]],
        memory_value: Any,
        retrieval_value: Any,
        problem_value: Any,
        degraded: Sequence[str],
    ) -> tuple[dict[str, Any], bytes]:
        envelope: dict[str, Any] = {
            "version": ENVELOPE_SCHEMA_VERSION,
            "project_id": str(project_id),
            "context_authority_generation": context_authority_generation,
            "privacy_generation": privacy_generation,
            "transcript_sequence": transcript_sequence,
            "ledger_revision": ledger_revision,
            "problem_model": problem_value,
            "decisions": decisions,
            "memory": memory_value,
            "memory_v2": retrieval_value,
            "recent_conversation": rows,
        }
        if degraded:
            envelope["degraded"] = list(degraded)
        return envelope, canonical_envelope_bytes(envelope)

    # The per-component byte caps can legitimately sum past the whole-envelope
    # cap (32 transcript rows alone may reach ~100KB), so an over-budget
    # envelope must degrade GRADUALLY: newest conversation rows are the one
    # component a public Fast turn cannot answer without, so they are shed
    # last and never below the newest few rows.  Each rung is deterministic;
    # the pre-2026-08 behavior of collapsing straight to a coreless
    # ``_truncated`` stub livelocked the typed Fast lane (its verifier
    # requires ``recent_conversation`` and the same over-budget transcript
    # rebuilt the same stub on every claim).
    memory_receipt = _envelope_budget_receipt(memory_projection, "memory")
    retrieval_receipt = _envelope_budget_receipt(retrieval_projection, "memory_v2")
    problem_receipt = _envelope_budget_receipt(problem, "problem_model")
    ladder: list[tuple[list[dict[str, Any]], Any, Any, Any, tuple[str, ...]]] = [
        (transcript_rows, memory_projection, retrieval_projection, problem, ()),
    ]
    for keep in (16, 8, 4):
        ladder.append(
            (
                transcript_rows[-keep:],
                memory_projection,
                retrieval_projection,
                problem,
                (f"transcript:last_{keep}",),
            )
        )
    ladder.append(
        (
            transcript_rows[-4:],
            memory_projection,
            retrieval_receipt,
            problem,
            ("transcript:last_4", "memory_v2:receipt"),
        )
    )
    ladder.append(
        (
            transcript_rows[-4:],
            memory_receipt,
            retrieval_receipt,
            problem,
            ("transcript:last_4", "memory:receipt", "memory_v2:receipt"),
        )
    )
    ladder.append(
        (
            transcript_rows[-4:],
            memory_receipt,
            retrieval_receipt,
            problem_receipt,
            (
                "transcript:last_4",
                "memory:receipt",
                "memory_v2:receipt",
                "problem_model:receipt",
            ),
        )
    )
    oversized_payload: bytes | None = None
    for rows, memory_value, retrieval_value, problem_value, degraded in ladder:
        _, payload = _assemble(
            rows, memory_value, retrieval_value, problem_value, degraded
        )
        if oversized_payload is None:
            oversized_payload = payload
        if len(payload) <= MAX_CANONICAL_ENVELOPE_BYTES:
            return CanonicalEnvelopeCore(payload, canonical_sha256(payload))
    # Unreachable in practice (the fully-receipted rung is far below the cap),
    # kept as the deterministic last resort: consumers must treat the
    # ``_truncated`` core as a degraded envelope, not a malformed one.
    envelope = {
        "version": ENVELOPE_SCHEMA_VERSION,
        "project_id": str(project_id),
        "context_authority_generation": context_authority_generation,
        "privacy_generation": privacy_generation,
        "transcript_sequence": transcript_sequence,
        "ledger_revision": ledger_revision,
        "_truncated": True,
        "_sha256": canonical_sha256(oversized_payload or b""),
        "_bytes": len(oversized_payload or b""),
    }
    payload = canonical_envelope_bytes(envelope)
    return CanonicalEnvelopeCore(payload, canonical_sha256(payload))


def _revision_identifier(value: Mapping[str, Any] | None) -> str:
    if isinstance(value, Mapping):
        revision = value.get("revision")
        if revision is not None and str(revision).strip():
            candidate = str(revision)
            if len(candidate) <= 128:
                return candidate
        return canonical_sha256(canonical_envelope_bytes(value))
    return "0"


def envelope_transport(snapshot: StrategicContextEnvelopeSnapshot) -> StrategicEnvelopeTransport:
    """Encode the stored blob once for transport without changing its bytes."""

    return StrategicEnvelopeTransport(
        snapshot_id=snapshot.snapshot_id,
        canonical_envelope_b64=base64.b64encode(snapshot.canonical_envelope).decode("ascii"),
        canonical_sha256=snapshot.canonical_sha256,
        context_authority_generation=snapshot.context_authority_generation,
        privacy_generation=snapshot.privacy_generation,
        transcript_sequence=snapshot.transcript_sequence,
        ledger_revision=snapshot.ledger_revision,
        expires_at=snapshot.expires_at,
    )


def context_envelope_reference(
    transport: StrategicEnvelopeTransport | Mapping[str, Any],
) -> dict[str, Any]:
    """Return the non-content pointer safe to persist in private packets.

    ``canonical_envelope_b64`` is intentionally absent.  The snapshot is the
    sole content-bearing cache and a packet must resolve it again at delivery.
    """

    value = (
        transport.model_dump(mode="json")
        if isinstance(transport, StrategicEnvelopeTransport)
        else dict(transport)
    )
    missing = [key for key in ENVELOPE_REFERENCE_FIELDS if key not in value]
    if missing:
        raise ValueError("incomplete strategic context envelope reference")
    return {key: value[key] for key in ENVELOPE_REFERENCE_FIELDS}


async def get_or_create_project_memory_state(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    lock: bool = False,
) -> StrategicMemoryProjectState:
    """Return the project's envelope fence row, creating the additive default."""

    query = select(StrategicMemoryProjectState).where(
        StrategicMemoryProjectState.project_id == project_id
    ).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    state = (await session.execute(query)).scalars().first()
    if state is not None:
        return state
    state = StrategicMemoryProjectState(project_id=project_id)
    try:
        # A savepoint keeps a duplicate lazy-state insert from poisoning the
        # enclosing conversation-claim transaction.
        async with session.begin_nested():
            session.add(state)
            await session.flush()
        return state
    except IntegrityError:
        query = select(StrategicMemoryProjectState).where(
            StrategicMemoryProjectState.project_id == project_id
        ).execution_options(populate_existing=True)
        if lock:
            query = query.with_for_update()
        state = (await session.execute(query)).scalars().first()
        if state is None:
            raise
        return state


async def bump_context_authority_generation(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> StrategicMemoryProjectState:
    """Atomically invalidate envelopes after an authority-bearing mutation."""

    state = await get_or_create_project_memory_state(session, project_id, lock=True)
    state.context_authority_generation += 1
    state.updated_at = _utcnow()
    session.add(state)
    await session.flush()
    return state


async def purge_expired_context_envelope_snapshots(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> int:
    """Hard-delete expired derived payloads; no conversational content lingers."""

    predicates = [
        StrategicContextEnvelopeSnapshot.expires_at <= (now or _utcnow())
    ]
    if project_id is not None:
        predicates.append(StrategicContextEnvelopeSnapshot.project_id == project_id)
    result = await session.execute(
        delete(StrategicContextEnvelopeSnapshot).where(*predicates)
    )
    return int(result.rowcount or 0)


async def bump_privacy_generation_and_purge_context_envelopes(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> StrategicMemoryProjectState:
    """Fence and erase derived context before a privacy mutation."""

    state = await get_or_create_project_memory_state(session, project_id, lock=True)
    state.privacy_generation += 1
    state.updated_at = _utcnow()
    session.add(state)
    await session.flush()
    await session.run_sync(
        _purge_derived_strategic_context_for_privacy_mutation,
        project_id,
    )
    return state


async def load_canonical_transcript_suffix(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    transcript_sequence: int,
    limit: int = DEFAULT_TRANSCRIPT_SUFFIX_LIMIT,
) -> list[ProjectMessage]:
    """Load the exact canonical conversation suffix ending at a known cursor."""

    if transcript_sequence < 0:
        raise ValueError("transcript_sequence must be nonnegative")
    if limit < 1:
        raise ValueError("transcript suffix limit must be positive")
    if transcript_sequence == 0:
        return []
    rows = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.sequence.is_not(None),
                ProjectMessage.sequence <= transcript_sequence,
                strategic_conversation_predicate(),
            )
            .order_by(ProjectMessage.sequence.desc(), ProjectMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    rows.reverse()
    if not rows or rows[-1].sequence != transcript_sequence:
        raise ContextEnvelopeFenceError(
            "canonical transcript cursor is absent or no longer visible"
        )
    return rows


async def store_context_envelope_snapshot(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    session_id: str,
    claim_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    input_revision: int,
    transcript_sequence: int,
    ledger_revision: int = 0,
    problem_model_revision: str = "0",
    decision_authority_revision: str = "0",
    context_authority_generation: int,
    privacy_generation: int,
    canonical_core: CanonicalEnvelopeCore,
    retrieval_trace: Mapping[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> StrategicContextEnvelopeSnapshot:
    """Persist or return the idempotent snapshot for one complete input fence."""

    values = (
        worker_generation,
        input_revision,
        transcript_sequence,
        ledger_revision,
        context_authority_generation,
        privacy_generation,
    )
    if any(value < 0 for value in values):
        raise ValueError("envelope generations and revisions must be nonnegative")
    existing = (
        await session.execute(
            select(StrategicContextEnvelopeSnapshot).where(
                StrategicContextEnvelopeSnapshot.project_id == project_id,
                StrategicContextEnvelopeSnapshot.session_id == session_id,
                StrategicContextEnvelopeSnapshot.claim_id == claim_id,
                StrategicContextEnvelopeSnapshot.worker_generation == worker_generation,
                StrategicContextEnvelopeSnapshot.worker_incarnation == worker_incarnation,
                StrategicContextEnvelopeSnapshot.input_revision == input_revision,
                StrategicContextEnvelopeSnapshot.transcript_sequence == transcript_sequence,
                StrategicContextEnvelopeSnapshot.ledger_revision == ledger_revision,
                StrategicContextEnvelopeSnapshot.problem_model_revision == problem_model_revision,
                StrategicContextEnvelopeSnapshot.decision_authority_revision == decision_authority_revision,
                StrategicContextEnvelopeSnapshot.context_authority_generation == context_authority_generation,
                StrategicContextEnvelopeSnapshot.privacy_generation == privacy_generation,
            )
        )
    ).scalars().first()
    if existing is not None:
        if (
            existing.canonical_sha256 != canonical_core.canonical_sha256
            or existing.canonical_envelope != canonical_core.canonical_envelope
        ):
            raise ContextEnvelopeFenceError(
                "same envelope input fence produced different canonical bytes"
            )
        return existing

    created_at = _utcnow()
    snapshot = StrategicContextEnvelopeSnapshot(
        project_id=project_id,
        session_id=session_id,
        claim_id=claim_id,
        worker_generation=worker_generation,
        worker_incarnation=worker_incarnation,
        input_revision=input_revision,
        transcript_sequence=transcript_sequence,
        ledger_revision=ledger_revision,
        problem_model_revision=problem_model_revision,
        decision_authority_revision=decision_authority_revision,
        context_authority_generation=context_authority_generation,
        privacy_generation=privacy_generation,
        canonical_envelope=canonical_core.canonical_envelope,
        canonical_sha256=canonical_core.canonical_sha256,
        retrieval_trace=dict(retrieval_trace or {}),
        created_at=created_at,
        expires_at=expires_at or created_at + DEFAULT_ENVELOPE_TTL,
    )
    try:
        # Concurrent Fast/Workday delivery is expected. The unique input fence
        # elects one stored blob; losers reload it and prove byte identity.
        async with session.begin_nested():
            session.add(snapshot)
            await session.flush()
        return snapshot
    except IntegrityError:
        existing = (
            await session.execute(
                select(StrategicContextEnvelopeSnapshot).where(
                    StrategicContextEnvelopeSnapshot.project_id == project_id,
                    StrategicContextEnvelopeSnapshot.session_id == session_id,
                    StrategicContextEnvelopeSnapshot.claim_id == claim_id,
                    StrategicContextEnvelopeSnapshot.worker_generation == worker_generation,
                    StrategicContextEnvelopeSnapshot.worker_incarnation == worker_incarnation,
                    StrategicContextEnvelopeSnapshot.input_revision == input_revision,
                    StrategicContextEnvelopeSnapshot.transcript_sequence == transcript_sequence,
                    StrategicContextEnvelopeSnapshot.ledger_revision == ledger_revision,
                    StrategicContextEnvelopeSnapshot.problem_model_revision == problem_model_revision,
                    StrategicContextEnvelopeSnapshot.decision_authority_revision == decision_authority_revision,
                    StrategicContextEnvelopeSnapshot.context_authority_generation == context_authority_generation,
                    StrategicContextEnvelopeSnapshot.privacy_generation == privacy_generation,
                )
            )
        ).scalars().first()
        if existing is None:
            raise
        if (
            existing.canonical_sha256 != canonical_core.canonical_sha256
            or existing.canonical_envelope != canonical_core.canonical_envelope
        ):
            raise ContextEnvelopeFenceError(
                "same envelope input fence produced different canonical bytes"
            )
        return existing


async def build_and_store_context_envelope_snapshot(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    session_id: str,
    claim_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    input_revision: int,
    transcript_sequence: int,
    decision_authority: Mapping[str, Any] | None,
    ledger_revision: int | None = None,
    problem_model_revision: str | None = None,
    decision_authority_revision: str | None = None,
    expected_context_authority_generation: int | None = None,
    expected_privacy_generation: int | None = None,
    retrieval_trace: Mapping[str, Any] | None = None,
    memory_query: str | None = None,
    attachment_manifests: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    expires_at: datetime | None = None,
    transcript_limit: int = DEFAULT_TRANSCRIPT_SUFFIX_LIMIT,
    _after_project_lock: (
        Callable[[Project], Awaitable[None] | None] | None
    ) = None,
) -> StrategicEnvelopeTransport:
    """Build current core context and persist it behind the live fence row.

    Snapshot retention is deliberately piggybacked on the active turn path in
    Phase 2: every envelope build reaps this project's expired derived blobs.
    This keeps the cache bounded without introducing a separate scheduler;
    canonical transcript and authority state remain the cold-restart source.
    """

    # Project-owned sources and their fence are one authority boundary. Normal
    # mutation routes already lock Project before their ``before_flush`` state
    # upsert, so take locks in that same Project -> state order. A concurrent
    # mutation is then either fully committed before our Project refresh or
    # waits before changing either source fields or its generation; reading
    # only after both locks prevents a stale source from acquiring a new fence.
    project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if project is None:
        raise ValueError("project does not exist")
    if _after_project_lock is not None:
        hook_result = _after_project_lock(project)
        if inspect.isawaitable(hook_result):
            await hook_result
    state = await get_or_create_project_memory_state(session, project_id, lock=True)
    await purge_expired_context_envelope_snapshots(session, project_id=project_id)
    # The controlled test seam (or a caller's pending unit of work) may have
    # changed Project after we first locked it. Refresh both source and state
    # under their already-held locks before using either in canonical bytes.
    project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    state = await get_or_create_project_memory_state(session, project_id, lock=True)
    if (
        expected_context_authority_generation is not None
        and state.context_authority_generation != expected_context_authority_generation
    ) or (
        expected_privacy_generation is not None
        and state.privacy_generation != expected_privacy_generation
    ):
        raise ContextEnvelopeFenceError("Strategic envelope authority or privacy fence changed")
    live_ledger_revision = int(state.ledger_revision)
    if ledger_revision is not None and ledger_revision != live_ledger_revision:
        raise ContextEnvelopeFenceError("Strategic envelope ledger revision changed")
    transcript = await load_canonical_transcript_suffix(
        session,
        project_id,
        transcript_sequence=transcript_sequence,
        limit=transcript_limit,
    )
    problem_model = project.problem_model if isinstance(project.problem_model, Mapping) else {}
    memory = (
        project.strategic_memory_notes
        if isinstance(project.strategic_memory_notes, list)
        else []
    )
    # V2 is an optional augmentation.  The established envelope core must
    # remain deliverable while it is disabled or a bounded retrieval read is
    # unavailable.  The flag is server-owned and defaults closed.
    retrieval_payload: Mapping[str, Any] | None = None
    retrieval_trace_payload: Mapping[str, Any] = {"v2_retrieval": "disabled"}
    from app.strategic_memory_governance import memory_v2_rollout

    rollout = memory_v2_rollout(project_id)
    if rollout.read_enabled or rollout.shadow_enabled:
        try:
            # The V2 retrieval pack is generated after Project -> state
            # fencing, so every card is from the exact revision pinned below.
            from app.strategic_memory_retrieval import retrieve_memory

            retrieval = await retrieve_memory(
                session,
                project_id=project_id,
                query=memory_query or "",
                source_sequence=int(state.incorporated_source_sequence),
                max_cards=6,
                raw_suffix_limit=MAX_RAW_SUFFIX_ROWS,
            )
            # Shadow runs may collect only content-free trace metrics.  They
            # never add cards to the persisted/shared Fast+Workday core.
            if rollout.read_enabled:
                retrieval_payload = retrieval.envelope_payload()
            retrieval_trace_payload = {
                **dict(retrieval.trace),
                "memory_degraded": retrieval.memory_degraded,
                "answerable": retrieval.answerable,
                "shadow": bool(rollout.shadow_enabled and not rollout.read_enabled),
            }
        except Exception:
            # Retrieval has no authority to block a live conversation.  Do not
            # record exception text because traces are durable derived state.
            logger.exception("strategic_memory_v2_retrieval_unavailable")
            retrieval_trace_payload = {"v2_retrieval": "unavailable"}
    core = build_canonical_envelope_core(
        project_id=project_id,
        context_authority_generation=state.context_authority_generation,
        privacy_generation=state.privacy_generation,
        transcript_sequence=transcript_sequence,
        ledger_revision=live_ledger_revision,
        problem_model=problem_model,
        decision_authority=decision_authority,
        memory=[item for item in memory if isinstance(item, Mapping)],
        transcript_suffix=transcript,
        attachment_manifests=attachment_manifests,
        retrieval=retrieval_payload,
    )
    snapshot = await store_context_envelope_snapshot(
        session,
        project_id=project_id,
        session_id=session_id,
        claim_id=claim_id,
        worker_generation=worker_generation,
        worker_incarnation=worker_incarnation,
        input_revision=input_revision,
        transcript_sequence=transcript_sequence,
        ledger_revision=live_ledger_revision,
        problem_model_revision=problem_model_revision or _revision_identifier(problem_model),
        decision_authority_revision=(
            decision_authority_revision or _revision_identifier(decision_authority)
        ),
        context_authority_generation=state.context_authority_generation,
        privacy_generation=state.privacy_generation,
        canonical_core=core,
        retrieval_trace={**retrieval_trace_payload, **dict(retrieval_trace or {})},
        expires_at=expires_at,
    )
    return envelope_transport(snapshot)


async def get_fenced_context_envelope_snapshot(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session_id: str | None = None,
    claim_id: uuid.UUID | None = None,
    worker_generation: int | None = None,
    worker_incarnation: uuid.UUID | None = None,
    now: datetime | None = None,
) -> StrategicEnvelopeTransport | None:
    """Return an unexpired snapshot only if its live project fences still match."""

    snapshot = (
        await session.execute(
            select(StrategicContextEnvelopeSnapshot)
            .where(StrategicContextEnvelopeSnapshot.snapshot_id == snapshot_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if snapshot is None or snapshot.project_id != project_id:
        return None
    if session_id is not None and snapshot.session_id != session_id:
        return None
    if claim_id is not None and snapshot.claim_id != claim_id:
        return None
    if worker_generation is not None and snapshot.worker_generation != worker_generation:
        return None
    if worker_incarnation is not None and snapshot.worker_incarnation != worker_incarnation:
        return None
    if snapshot.expires_at <= (now or _utcnow()):
        return None
    state = (
        await session.execute(
            select(StrategicMemoryProjectState)
            .where(StrategicMemoryProjectState.project_id == project_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if (
        state is None
        or state.context_authority_generation != snapshot.context_authority_generation
        or state.privacy_generation != snapshot.privacy_generation
    ):
        return None
    if canonical_sha256(snapshot.canonical_envelope) != snapshot.canonical_sha256:
        return None
    return envelope_transport(snapshot)


async def get_fenced_context_envelope_by_reference(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    reference: Mapping[str, Any],
    now: datetime | None = None,
) -> StrategicEnvelopeTransport | None:
    """Resolve a packet pointer only when its exact snapshot is still current.

    Packet metadata is deliberately redundant with the snapshot fence.  The
    comparison makes a substituted/stale pointer fail closed even when a UUID
    happens to be valid for the project, while the underlying read verifies the
    live authority/privacy generations and byte hash.
    """

    try:
        snapshot_id = uuid.UUID(str(reference["snapshot_id"]))
        expected_generation = int(reference["context_authority_generation"])
        expected_privacy = int(reference["privacy_generation"])
        expected_sequence = int(reference["transcript_sequence"])
        expected_ledger = int(reference["ledger_revision"])
        expected_sha256 = str(reference["canonical_sha256"])
        # Expiry is retained in the pointer for observability, but it is not an
        # immutable identity field: a durable packet can safely extend the
        # snapshot's retention from Fast's short TTL to the bounded packet TTL.
        # The fenced snapshot row below is the sole authority for whether it is
        # currently deliverable.
        expected_expiry = str(reference["expires_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if (
        expected_generation < 0
        or expected_privacy < 0
        or expected_sequence < 0
        or expected_ledger < 0
        or len(expected_sha256) != 64
        or not expected_expiry
    ):
        return None
    transport = await get_fenced_context_envelope_snapshot(
        session,
        project_id=project_id,
        snapshot_id=snapshot_id,
        now=now,
    )
    if transport is None:
        return None
    actual = context_envelope_reference(transport)
    if (
        str(actual["snapshot_id"]) != str(snapshot_id)
        or actual["canonical_sha256"] != expected_sha256
        or actual["context_authority_generation"] != expected_generation
        or actual["privacy_generation"] != expected_privacy
        or actual["transcript_sequence"] != expected_sequence
        or actual["ledger_revision"] != expected_ledger
    ):
        return None
    return transport


async def pin_fenced_context_envelope_for_packet(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    reference: Mapping[str, Any],
    now: datetime | None = None,
    _after_state_lock: (
        Callable[[StrategicMemoryProjectState], Awaitable[None] | None] | None
    ) = None,
) -> StrategicEnvelopeTransport | None:
    """Extend a current envelope for one durable private hand-off, bounded at 7d.

    The state fence is locked before the snapshot row, matching the privacy
    invalidation path (state bump, then snapshot deletion).  The pointer is
    compared *after* both locks are held: a generation bump cannot land between
    an optimistic lookup and the retained packet write.  A stale/redacted/
    expired reference is rejected rather than resurrected.  Terminal packet
    retention is handled by the global reaper; this pin only protects the
    restart-safe live hand-off.

    ``_after_state_lock`` is an internal regression-test seam used to simulate
    a mutation that becomes visible between the two row locks; production
    callers must not pass it.
    """
    try:
        snapshot_id = uuid.UUID(str(reference["snapshot_id"]))
        expected_generation = int(reference["context_authority_generation"])
        expected_privacy = int(reference["privacy_generation"])
        expected_sequence = int(reference["transcript_sequence"])
        expected_ledger = int(reference["ledger_revision"])
        expected_sha256 = str(reference["canonical_sha256"])
        expected_expiry = str(reference["expires_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if (
        expected_generation < 0
        or expected_privacy < 0
        or expected_sequence < 0
        or expected_ledger < 0
        or len(expected_sha256) != 64
        or not expected_expiry
    ):
        return None

    # Deterministic write lock order: StrategicMemoryProjectState first,
    # StrategicContextEnvelopeSnapshot second.  The privacy/authority mutation
    # hook obtains the same state row before deleting envelope rows.
    state = (
        await session.execute(
            select(StrategicMemoryProjectState)
            .where(StrategicMemoryProjectState.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if state is None:
        return None
    if _after_state_lock is not None:
        hook_result = _after_state_lock(state)
        if inspect.isawaitable(hook_result):
            await hook_result

    snapshot = (
        await session.execute(
            select(StrategicContextEnvelopeSnapshot)
            .where(
                StrategicContextEnvelopeSnapshot.project_id == project_id,
                StrategicContextEnvelopeSnapshot.snapshot_id == snapshot_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if snapshot is None:
        return None
    current_now = now or _utcnow()
    if (
        snapshot.expires_at <= current_now
        or state.context_authority_generation != expected_generation
        or state.privacy_generation != expected_privacy
        or snapshot.context_authority_generation != expected_generation
        or snapshot.privacy_generation != expected_privacy
        or snapshot.transcript_sequence != expected_sequence
        or snapshot.ledger_revision != expected_ledger
        or snapshot.canonical_sha256 != expected_sha256
        or canonical_sha256(snapshot.canonical_envelope) != expected_sha256
    ):
        return None
    pinned_until = min(
        snapshot.created_at + MAX_PACKET_PINNED_ENVELOPE_TTL,
        current_now + MAX_PACKET_PINNED_ENVELOPE_TTL,
    )
    if pinned_until > snapshot.expires_at:
        snapshot.expires_at = pinned_until
        session.add(snapshot)
        await session.flush()
    return envelope_transport(snapshot)


__all__ = [
    "CanonicalEnvelopeCore",
    "ContextEnvelopeFenceError",
    "StrategicEnvelopeTransport",
    "build_and_store_context_envelope_snapshot",
    "build_canonical_envelope_core",
    "bump_context_authority_generation",
    "bump_privacy_generation_and_purge_context_envelopes",
    "canonical_attachment_manifest",
    "canonical_envelope_bytes",
    "canonical_memory_projection",
    "canonical_sha256",
    "canonical_transcript_suffix",
    "context_envelope_reference",
    "envelope_transport",
    "ENVELOPE_REFERENCE_FIELDS",
    "get_fenced_context_envelope_by_reference",
    "get_fenced_context_envelope_snapshot",
    "get_or_create_project_memory_state",
    "load_canonical_transcript_suffix",
    "MAX_PACKET_PINNED_ENVELOPE_TTL",
    "pin_fenced_context_envelope_for_packet",
    "purge_expired_context_envelope_snapshots",
    "store_context_envelope_snapshot",
]
