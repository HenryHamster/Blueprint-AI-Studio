"""Unit coverage for deterministic Phase-3 source parsing primitives."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib

import pytest

from app.strategic_memory_sources import (
    SourceReceiptInput,
    SourceResolutionError,
    SourceValidationError,
    canonical_json_bytes,
    parse_exact_spans,
    record_source,
    resolve_source_receipt,
    sha256_hex,
    verify_exact_span,
)


def test_canonical_json_bytes_is_stable_and_rejects_coercive_inputs() -> None:
    assert canonical_json_bytes({"z": [True, None], "a": "é"}) == (
        b'{"a":"\xc3\xa9","z":[true,null]}'
    )
    assert canonical_json_bytes({"n": -0.0}) == b'{"n":0}'
    assert canonical_json_bytes({"unit": 1.0, "whole": 2.0}) == (
        b'{"unit":1,"whole":2}'
    )
    with pytest.raises(SourceValidationError, match="string object keys"):
        canonical_json_bytes({1: "not silently stringified"})
    with pytest.raises(SourceValidationError, match="NaN"):
        canonical_json_bytes({"n": float("nan")})
    with pytest.raises(SourceValidationError, match="does not support"):
        canonical_json_bytes({"bad": b"bytes"})


def test_exact_parser_uses_utf8_byte_offsets_and_keeps_raw_math() -> None:
    payload = "α before $x_{t+1} \\leq -2.5$ and `not math`".encode("utf-8")
    spans = parse_exact_spans(payload)
    latex = next(span for span in spans if span.kind == "latex_inline")
    assert latex.raw_text == "$x_{t+1} \\leq -2.5$"
    assert latex.byte_start == len("α before ".encode("utf-8"))
    assert latex.byte_end == latex.byte_start + len(latex.raw_text.encode("utf-8"))
    assert latex.span_hash == hashlib.sha256(latex.raw_text.encode("utf-8")).hexdigest()
    assert latex.normalized_forms[0] == "latex-v1:$ x _ { t + 1 } \\leq - 2.5 $"
    assert verify_exact_span(
        payload,
        byte_start=latex.byte_start,
        byte_end=latex.byte_end,
        span_hash=latex.span_hash,
    ) == latex.raw_text.encode("utf-8")


def test_parser_finds_math_code_identifier_number_and_citations() -> None:
    text = """```python
loss_fn(theta) <= -1.0e-2
```
$$\\forall x \\in \\mathbb{R}: f(x) = x^2$$
See src/model.py:17, commit deadbeef, task 123e4567-e89b-12d3-a456-426614174000.
"""
    spans = parse_exact_spans(text)
    by_kind = {kind: [s for s in spans if s.kind == kind] for kind in {s.kind for s in spans}}
    assert by_kind["code_fence"]
    assert any(span.raw_text == "loss_fn" for span in by_kind["identifier"])
    assert any(span.raw_text == "-1.0e-2" for span in by_kind["number"])
    assert by_kind["latex_display"]
    citations = {span.raw_text for span in by_kind["citation"]}
    assert "src/model.py:17" in citations
    assert "deadbeef" in citations
    assert "123e4567-e89b-12d3-a456-426614174000" in citations


def test_exact_span_fails_closed_for_split_multibyte_or_hash_mismatch() -> None:
    payload = "π=3".encode("utf-8")
    with pytest.raises(SourceResolutionError, match="splits UTF-8"):
        verify_exact_span(payload, byte_start=1, byte_end=2, span_hash=sha256_hex(payload[1:2]))
    with pytest.raises(SourceResolutionError, match="hash mismatch"):
        verify_exact_span(payload, byte_start=0, byte_end=2, span_hash="0" * 64)


@pytest.mark.asyncio
async def test_native_message_receipt_is_idempotent_and_fails_closed(db_session) -> None:
    from app.models import Project, ProjectMessage, StrategicMemorySource

    project = Project(name="source test", user_id="source-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=11,
        from_type="user",
        content="α must remain exact",
    )
    db_session.add(message)
    await db_session.flush()
    receipt_input = SourceReceiptInput(
        project_id=project.id,
        source_type="project_message",
        source_key=str(message.id),
        source_revision=str(message.sequence),
        content_hash=sha256_hex(message.content),
        native_sequence=message.sequence,
        occurred_at=message.created_at,
        actor_kind="user",
        actor_id=None,
        metadata={"surface": "activity"},
    )
    # The canonical ProjectMessage writer dual-writes this native receipt in
    # the same transaction. Calling the service again proves the idempotent
    # identity path rather than allocating a second source sequence.
    first = await record_source(db_session, receipt_input)
    assert first.created is False
    assert first.source.source_sequence == 1
    assert first.disposition.status == "pending"
    duplicate = await record_source(db_session, receipt_input)
    assert duplicate.created is False
    assert duplicate.source.id == first.source.id
    resolved = await resolve_source_receipt(
        db_session, first.source, expected_project_id=project.id
    )
    assert resolved.canonical_payload == message.content.encode("utf-8")

    mutable_payload = canonical_json_bytes({"revision": 1, "intent": "exact"})
    mutable_input = SourceReceiptInput(
        project_id=project.id,
        source_type="problem_model_revision",
        source_key=str(project.id),
        source_revision="1",
        content_hash=sha256_hex(mutable_payload),
        occurred_at=message.created_at,
        canonical_payload=mutable_payload,
        mutable=True,
    )
    second = await record_source(db_session, mutable_input)
    assert second.created is True
    assert second.source.source_sequence == 2
    assert (
        await resolve_source_receipt(db_session, second.source, expected_project_id=project.id)
    ).canonical_payload == mutable_payload

    # A receipt key alone is never enough to authorize a retry: every
    # immutable audit/security fact must agree with the original receipt.
    for changed in (
        {"ingestion_origin": "backfill"},
        {"actor_kind": "operator"},
        {"actor_id": "someone-else"},
        {"resident_session_id": "other-session"},
        {"visibility": "private"},
        {"sensitivity": "restricted"},
        {"metadata": {"field": "other"}},
        {"occurred_at": mutable_input.occurred_at + timedelta(seconds=1)},
    ):
        with pytest.raises(SourceValidationError, match="different immutable receipt"):
            await record_source(db_session, replace(mutable_input, **changed))

    conflicting_payload = canonical_json_bytes({"revision": 1, "intent": "different"})
    conflicting = SourceReceiptInput(
        **{
            **mutable_input.__dict__,
            "content_hash": sha256_hex(conflicting_payload),
            "canonical_payload": conflicting_payload,
        }
    )
    with pytest.raises(SourceValidationError, match="different immutable receipt"):
        await record_source(db_session, conflicting)
    with pytest.raises(SourceResolutionError, match="cross-project"):
        await resolve_source_receipt(db_session, first.source, expected_project_id=__import__("uuid").uuid4())

    # The source table must not have copied immutable native content.
    stored = await db_session.get(StrategicMemorySource, first.source.id)
    assert stored is not None and stored.canonical_payload is None
