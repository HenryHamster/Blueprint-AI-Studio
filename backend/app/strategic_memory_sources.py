"""Immutable Strategic-memory source receipts and byte-exact source parsing.

This module deliberately has no HTTP concerns and never commits a session.  A
canonical writer owns the surrounding transaction, calls ``record_source``
after it has persisted its native object, and commits/broadcasts only after
both writes have succeeded.  The same is true for resolution: the receiver
must provide the project it is authorized to read and a receipt can never be
used as a cross-project content pointer.

The parser is intentionally conservative.  It finds deterministic candidate
spans for retrieval and validation, rather than claiming that syntactically
similar mathematics are semantically equivalent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, ProjectMessage, StrategicMemoryProjectState
from app.time_utils import _utcnow

if TYPE_CHECKING:  # Avoid making the Phase-3 models an import-time cycle.
    from app.models import StrategicMemorySource


SourceOrigin = Literal["live", "backfill"]


class SourceValidationError(ValueError):
    """A source receipt is malformed, stale, or not safe to use."""


class SourceResolutionError(SourceValidationError):
    """The native source no longer resolves to the immutable receipt."""


@dataclass(frozen=True)
class SourceReceiptInput:
    """Input to the sole source-receipt allocation service.

    ``canonical_payload`` is for mutable/non-versioned sources only.  Native
    immutable sources (notably ``project_message``) must omit it; copying a
    message into this table would create an ungoverned second content store.
    """

    project_id: uuid.UUID
    source_type: str
    source_key: str
    source_revision: str
    content_hash: str
    occurred_at: datetime
    native_sequence: int | None = None
    ingestion_origin: SourceOrigin = "live"
    actor_kind: str | None = None
    actor_id: str | None = None
    resident_session_id: str | None = None
    visibility: str = "project"
    sensitivity: str = "normal"
    metadata: Mapping[str, Any] | None = None
    canonical_payload: bytes | str | None = None
    mutable: bool = False


@dataclass(frozen=True)
class ResolvedSource:
    project_id: uuid.UUID
    source_type: str
    source_key: str
    source_revision: str
    content_hash: str
    canonical_payload: bytes
    visibility: str
    sensitivity: str


@dataclass(frozen=True)
class SourceReceiptResult:
    source: Any
    disposition: Any
    created: bool


@dataclass(frozen=True)
class ExactSpan:
    """A half-open UTF-8 byte span and independent retrieval lookup forms."""

    kind: str
    byte_start: int
    byte_end: int
    raw_text: str
    span_hash: str
    normalized_forms: tuple[str, ...]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"(?<![\w.])[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?![\w.])")
_NUMBER_RE = re.compile(
    r"(?<![\w.])(?:[+\-−])?(?:\d+(?:_\d+)*(?:\.\d+(?:_\d+)*)?|\.\d+(?:_\d+)*)(?:[eE][+\-]?\d+)?(?![\w.])"
)
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_COMMIT_RE = re.compile(r"\b[0-9a-fA-F]{7,64}\b")
_URL_RE = re.compile(r"https?://[^\s<>\])}]+")
_FILE_REF_RE = re.compile(r"(?<!\w)(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?::\d+(?::\d+)?)?")
_MATH_SYMBOL_RE = re.compile(r"[∀∃∑∏∫∂∇∞≈≠≤≥±∓×÷→←↔⇒⇔⊂⊆∈∉∧∨¬⊕⊗⋅⋆√∝ℝℕℤℚℂα-ωΑ-Ω₀-₉⁰-⁹]+")
_QUANTIFIER_OR_COMPARATOR_RE = re.compile(r"(?:∀|∃|≤|≥|<|>|=|≠|≈|∈|∉|∧|∨|¬|⇒|⇔|[₀-₉⁰-⁹])+")
_FENCE_RE = re.compile(r"(?ms)^```(?P<lang>math|latex|tex|[A-Za-z0-9_-]*)[ \t]*\n(?P<body>.*?)(?:\n```|\Z)")
_DISPLAY_DOLLAR_RE = re.compile(r"(?s)\$\$(.+?)\$\$")
_DISPLAY_BRACKET_RE = re.compile(r"(?s)\\\[(.+?)\\\]")
_INLINE_DOLLAR_RE = re.compile(r"(?<!\\)\$(?!\$)([^\n$]+?)(?<!\\)\$(?!\$)")
_INLINE_PAREN_RE = re.compile(r"(?s)\\\((.+?)\\\)")
_LATEX_TOKEN_RE = re.compile(r"\\[A-Za-z]+|\\.|[A-Za-z]+|\d+(?:\.\d+)?|[{}()[\]^_]|[+\-−=<>≤≥≈≠,.;:/]|[^\s]")


def sha256_hex(payload: bytes | str) -> str:
    """Return the lower-case SHA-256 of exact UTF-8 data."""

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_number(value: float) -> str:
    """Conservative JCS-like number encoding for finite IEEE-754 values.

    Python's shortest round-trippable representation is sufficient for the
    supported JSON subset here.  We additionally remove exponent zero-padding
    and normalize negative zero; callers that need arbitrary-precision decimal
    semantics must encode those values as strings rather than silently losing
    precision in JSON.
    """

    if not math.isfinite(value):
        raise SourceValidationError("canonical JSON rejects NaN and infinity")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    rendered = repr(value)
    if "e" not in rendered and "E" not in rendered:
        return rendered
    mantissa, exponent = rendered.lower().split("e", 1)
    exponent_value = int(exponent)
    # RFC 8785 follows ECMAScript's fixed form between 1e-6 and 1e21.
    # Derive it from Python's already-shortest mantissa instead of formatting
    # to a fixed precision (which would round 1.2345678901234567 incorrectly).
    absolute = abs(value)
    if 1e-6 <= absolute < 1e21:
        sign = ""
        if mantissa.startswith("-"):
            sign, mantissa = "-", mantissa[1:]
        integer, dot, fraction = mantissa.partition(".")
        digits = integer + fraction
        decimal_at = len(integer) + exponent_value
        if decimal_at <= 0:
            fixed = sign + "0." + ("0" * -decimal_at) + digits
        elif decimal_at >= len(digits):
            fixed = sign + digits + ("0" * (decimal_at - len(digits)))
        else:
            fixed = sign + digits[:decimal_at] + "." + digits[decimal_at:]
        fixed = fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        return fixed if fixed not in {"-0", ""} else "0"
    return f"{mantissa}e{exponent_value:+d}".replace("e+", "e+")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a supported JSON value deterministically without coercion.

    This intentionally rejects mapping keys other than strings, non-finite
    floats, bytes, arbitrary objects, and duplicate semantic coercions.  It is
    RFC-8785-like for ordinary JSON values and is used only for immutable
    receipt snapshots; source text itself always bypasses this serializer.
    """

    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, int) and not isinstance(item, bool):
            return str(item)
        if isinstance(item, float):
            return _canonical_number(item)
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise SourceValidationError("canonical JSON requires string object keys")
            return "{" + ",".join(
                f"{encode(key)}:{encode(item[key])}" for key in sorted(item.keys())
            ) + "}"
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(element) for element in item) + "]"
        raise SourceValidationError(
            f"canonical JSON does not support {type(item).__name__}; use a JSON value"
        )

    return encode(value).encode("utf-8")


def _payload_bytes(value: bytes | str | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        # Receipts are text/JSON only.  Reject arbitrary binary bytes rather
        # than treating invalid UTF-8 as a different canonical text dialect.
        try:
            value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceValidationError("canonical payload must be UTF-8") from exc
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise SourceValidationError("canonical payload must be UTF-8 bytes or text")


def _validate_input(source: SourceReceiptInput) -> bytes | None:
    if source.ingestion_origin not in {"live", "backfill"}:
        raise SourceValidationError("unknown ingestion origin")
    if not source.source_type or not source.source_key or not source.source_revision:
        raise SourceValidationError("source type, key, and revision are required")
    if source.native_sequence is not None and source.native_sequence < 0:
        raise SourceValidationError("native sequence must be nonnegative")
    if not _SHA256_RE.fullmatch(source.content_hash):
        raise SourceValidationError("content hash must be lower-case SHA-256")
    payload = _payload_bytes(source.canonical_payload)
    if source.mutable and payload is None:
        raise SourceValidationError("mutable source requires canonical payload")
    if not source.mutable and payload is not None:
        raise SourceValidationError("immutable source must resolve natively, not copy payload")
    if payload is not None and sha256_hex(payload) != source.content_hash:
        raise SourceValidationError("canonical payload hash does not match receipt")
    return payload


async def _phase3_models() -> tuple[type[Any], type[Any]]:
    """Resolve Phase-3 models lazily so this module can be imported in PR order."""

    from app.models import StrategicMemorySource, StrategicMemorySourceDisposition

    return StrategicMemorySource, StrategicMemorySourceDisposition


async def lock_or_create_source_project_state(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> StrategicMemoryProjectState:
    """Establish and lock the receipt allocator row without a first-use race."""

    # Envelope construction and every authority mutation use Project -> state.
    # Enforce that order inside the shared allocator so backfill and maintenance
    # callers cannot introduce the inverse state -> Project edge.
    project_exists = await session.scalar(
        select(Project.id)
        .where(Project.id == project_id)
        .with_for_update()
    )
    if project_exists is None:
        raise SourceValidationError("source project does not exist")
    table = StrategicMemoryProjectState.__table__
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - Blueprint supports PostgreSQL; SQLite is tests.
        raise SourceValidationError(
            "source state allocation requires PostgreSQL or SQLite ON CONFLICT"
        )
    # The insert and later receipt stay in the caller transaction. Concurrent
    # first users serialize on the project-state PK instead of poisoning one
    # transaction with an unhandled uniqueness failure.
    await session.execute(
        dialect_insert(table)
        .values(project_id=project_id)
        .on_conflict_do_nothing(index_elements=[table.c.project_id])
    )
    state = (
        await session.execute(
            select(StrategicMemoryProjectState)
            .where(StrategicMemoryProjectState.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if state is None:  # pragma: no cover - FK/upsert invariant
        raise SourceValidationError("failed to establish source project state")
    return state


def _canonical_utc(value: datetime) -> datetime:
    """Compare occurrence times in the database's naive-UTC representation."""

    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _canonical_metadata(value: Mapping[str, Any] | None) -> bytes:
    """Make replay comparison reject even subtle JSON-shape drift."""

    return canonical_json_bytes(dict(value or {}))


def _same_receipt(existing: Any, source: SourceReceiptInput, payload: bytes | None) -> bool:
    """Return true only for an exact replay of all persisted receipt facts."""

    existing_payload = getattr(existing, "canonical_payload", None)
    if existing_payload is not None:
        existing_payload = bytes(existing_payload)
    return (
        existing.project_id == source.project_id
        and existing.source_type == source.source_type
        and existing.source_key == source.source_key
        and existing.source_revision == source.source_revision
        and existing.content_hash == source.content_hash
        and existing.native_sequence == source.native_sequence
        and existing.ingestion_origin == source.ingestion_origin
        and _canonical_utc(existing.occurred_at) == _canonical_utc(source.occurred_at)
        and existing.actor_kind == source.actor_kind
        and existing.actor_id == source.actor_id
        and existing.resident_session_id == source.resident_session_id
        and existing.visibility == source.visibility
        and existing.sensitivity == source.sensitivity
        and _canonical_metadata(getattr(existing, "source_metadata", {}))
        == _canonical_metadata(source.metadata)
        and existing_payload == payload
        and getattr(existing, "payload_state", "active") == "active"
        and getattr(existing, "tombstoned_at", None) is None
    )


async def record_source(
    session: AsyncSession,
    source: SourceReceiptInput,
) -> SourceReceiptResult:
    """Idempotently allocate one immutable source receipt in the caller txn.

    The caller must already have written its native source object.  This
    helper locks the per-project state row, assigns the next source sequence,
    inserts an explicit pending disposition, and flushes.  It never commits or
    broadcasts, preserving all-or-nothing writer semantics.
    """

    payload = _validate_input(source)
    StrategicMemorySource, StrategicMemorySourceDisposition = await _phase3_models()
    existing = (
        await session.execute(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == source.project_id,
                StrategicMemorySource.source_type == source.source_type,
                StrategicMemorySource.source_key == source.source_key,
                StrategicMemorySource.source_revision == source.source_revision,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not _same_receipt(existing, source, payload):
            raise SourceValidationError("duplicate source identity has different immutable receipt")
        disposition = await session.get(StrategicMemorySourceDisposition, existing.id)
        if disposition is None:
            raise SourceValidationError("source receipt is missing its required disposition")
        return SourceReceiptResult(existing, disposition, False)

    state = await lock_or_create_source_project_state(session, source.project_id)

    # A concurrent caller can have performed its first duplicate probe before
    # waiting on this project-state lock.  Re-check under the lock so a live
    # receipt wins over backfill (and a retry does not become a unique error).
    existing = (
        await session.execute(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == source.project_id,
                StrategicMemorySource.source_type == source.source_type,
                StrategicMemorySource.source_key == source.source_key,
                StrategicMemorySource.source_revision == source.source_revision,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not _same_receipt(existing, source, payload):
            raise SourceValidationError("duplicate source identity has different immutable receipt")
        disposition = await session.get(StrategicMemorySourceDisposition, existing.id)
        if disposition is None:
            raise SourceValidationError("source receipt is missing its required disposition")
        return SourceReceiptResult(existing, disposition, False)

    next_sequence = int(getattr(state, "source_high_watermark", 0)) + 1
    receipt = StrategicMemorySource(
        project_id=source.project_id,
        source_sequence=next_sequence,
        native_sequence=source.native_sequence,
        ingestion_origin=source.ingestion_origin,
        source_type=source.source_type,
        source_key=source.source_key,
        source_revision=source.source_revision,
        content_hash=source.content_hash,
        actor_kind=source.actor_kind,
        actor_id=source.actor_id,
        occurred_at=source.occurred_at,
        recorded_at=_utcnow(),
        resident_session_id=source.resident_session_id,
        visibility=source.visibility,
        sensitivity=source.sensitivity,
        payload_state="active",
        source_metadata=dict(source.metadata or {}),
        canonical_payload=payload,
    )
    disposition = StrategicMemorySourceDisposition(
        source_id=receipt.id,
        status="pending",
        reason=None,
        classifier_version=None,
        extractor_model=None,
        prompt_version=None,
        consolidation_job_id=None,
        processed_at=None,
    )
    # The sequence update and both inserts are intentionally in one flush.
    state.source_high_watermark = next_sequence
    session.add(receipt)
    session.add(disposition)
    try:
        await session.flush()
    except IntegrityError as exc:
        # A unique race can only be safely reconciled within a savepoint owned
        # by a caller that opted into one.  Do not catch-and-commit here: that
        # would corrupt the surrounding native writer transaction.
        raise SourceValidationError("concurrent source receipt conflict; retry transaction") from exc
    return SourceReceiptResult(receipt, disposition, True)


async def resolve_source_receipt(
    session: AsyncSession,
    source: Any,
    *,
    expected_project_id: uuid.UUID,
) -> ResolvedSource:
    """Resolve one receipt, proving scope, bytes, revision, and hash first."""

    if source.project_id != expected_project_id:
        raise SourceResolutionError("cross-project source access denied")
    if getattr(source, "payload_state", "active") != "active":
        raise SourceResolutionError("source is redacted or deleted")

    canonical_payload = getattr(source, "canonical_payload", None)
    if canonical_payload is not None:
        payload = bytes(canonical_payload)
    elif source.source_type == "project_message":
        try:
            message_id = uuid.UUID(str(source.source_key))
        except (TypeError, ValueError) as exc:
            raise SourceResolutionError("project-message source key is not a UUID") from exc
        message = await session.get(ProjectMessage, message_id)
        if message is None or message.project_id != expected_project_id:
            raise SourceResolutionError("project-message source is unavailable")
        if message.sequence is None or str(message.sequence) != str(source.source_revision):
            raise SourceResolutionError("project-message source revision mismatch")
        # Governed privacy deletion/redaction uses the receipt payload state;
        # this additional marker protects native tombstones during migration.
        if (message.context or {}).get("privacy_state") in {"redacted", "deleted"}:
            raise SourceResolutionError("project-message source was redacted")
        payload = message.content.encode("utf-8")
    else:
        raise SourceResolutionError("source lacks canonical payload and native resolver")

    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceResolutionError("resolved source is not canonical UTF-8") from exc
    if sha256_hex(payload) != source.content_hash:
        raise SourceResolutionError("resolved source content hash mismatch")
    return ResolvedSource(
        project_id=source.project_id,
        source_type=source.source_type,
        source_key=source.source_key,
        source_revision=source.source_revision,
        content_hash=source.content_hash,
        canonical_payload=payload,
        visibility=source.visibility,
        sensitivity=source.sensitivity,
    )


def _byte_offsets(text: str) -> list[int]:
    """Map Python character indexes to exact UTF-8 byte indexes."""

    offsets = [0]
    total = 0
    for character in text:
        total += len(character.encode("utf-8"))
        offsets.append(total)
    return offsets


def _latex_lookup(raw: str) -> tuple[str, ...]:
    tokens = tuple(_LATEX_TOKEN_RE.findall(raw))
    if not tokens:
        return ()
    # Preserve all grammar-significant token boundaries; aliases are lookup
    # candidates only and are never used as byte-exact identity.
    return ("latex-v1:" + " ".join(tokens), "formula-v1:" + sha256_hex("\x1f".join(tokens)))


def _normal_forms(kind: str, raw: str) -> tuple[str, ...]:
    if kind in {"latex_inline", "latex_display", "latex_fence", "math_symbol"}:
        return _latex_lookup(raw)
    if kind == "identifier":
        return ("identifier-v1:" + unicodedata.normalize("NFC", raw).casefold(),)
    if kind == "number":
        return ("number-v1:" + raw.replace("−", "-").replace("_", ""),)
    if kind == "citation":
        return ("citation-v1:" + raw.casefold(),)
    if kind == "code_fence":
        return ("code-v1:" + sha256_hex(raw),)
    if kind == "math_operator":
        return ("math-operator-v1:" + raw,)
    return ()


def parse_exact_spans(payload: bytes | str) -> list[ExactSpan]:
    """Extract deterministic candidate spans with half-open UTF-8 byte offsets."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceValidationError("exact span parser requires UTF-8") from exc
        raw_bytes = payload
    elif isinstance(payload, str):
        text = payload
        raw_bytes = text.encode("utf-8")
    else:
        raise TypeError("payload must be UTF-8 bytes or text")

    offsets = _byte_offsets(text)
    seen: set[tuple[str, int, int]] = set()
    result: list[ExactSpan] = []

    def add(kind: str, start: int, end: int) -> None:
        if start >= end:
            return
        key = (kind, start, end)
        if key in seen:
            return
        seen.add(key)
        byte_start = offsets[start]
        byte_end = offsets[end]
        span_bytes = raw_bytes[byte_start:byte_end]
        # The two slicing views must agree.  This guards accidental character
        # offsets in future parser changes, especially before non-ASCII math.
        if span_bytes != text[start:end].encode("utf-8"):
            raise AssertionError("UTF-8 span offset invariant violated")
        raw = text[start:end]
        result.append(
            ExactSpan(
                kind=kind,
                byte_start=byte_start,
                byte_end=byte_end,
                raw_text=raw,
                span_hash=sha256_hex(span_bytes),
                normalized_forms=_normal_forms(kind, raw),
            )
        )

    for match in _FENCE_RE.finditer(text):
        language = (match.group("lang") or "").casefold()
        add("latex_fence" if language in {"math", "latex", "tex"} else "code_fence", match.start(), match.end())
    for regex, kind in (
        (_DISPLAY_DOLLAR_RE, "latex_display"),
        (_DISPLAY_BRACKET_RE, "latex_display"),
        (_INLINE_DOLLAR_RE, "latex_inline"),
        (_INLINE_PAREN_RE, "latex_inline"),
        (_MATH_SYMBOL_RE, "math_symbol"),
        (_QUANTIFIER_OR_COMPARATOR_RE, "math_operator"),
        (_IDENTIFIER_RE, "identifier"),
        (_NUMBER_RE, "number"),
        (_URL_RE, "citation"),
        (_UUID_RE, "citation"),
        (_FILE_REF_RE, "citation"),
        (_COMMIT_RE, "citation"),
    ):
        for match in regex.finditer(text):
            add(kind, match.start(), match.end())
    return sorted(result, key=lambda span: (span.byte_start, span.byte_end, span.kind))


def verify_exact_span(
    payload: bytes | str,
    *,
    byte_start: int,
    byte_end: int,
    span_hash: str,
) -> bytes:
    """Return a verified raw span or fail closed on any bounds/hash defect."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes):
        raise TypeError("payload must be UTF-8 bytes or text")
    if byte_start < 0 or byte_end < byte_start or byte_end > len(raw):
        raise SourceResolutionError("exact span bounds are invalid")
    span = raw[byte_start:byte_end]
    try:
        # Ensure neither offset splits a UTF-8 sequence.
        raw[:byte_start].decode("utf-8")
        span.decode("utf-8")
        raw[byte_end:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceResolutionError("exact span splits UTF-8 codepoint") from exc
    if not _SHA256_RE.fullmatch(span_hash) or sha256_hex(span) != span_hash:
        raise SourceResolutionError("exact span hash mismatch")
    return span
