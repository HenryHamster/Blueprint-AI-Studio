"""Focused exact/lexical retrieval tests for the lexical-only V2 baseline."""

from __future__ import annotations

import uuid

import pytest

from app.models import (
    Project,
    StrategicMemoryCorrectionBarrier,
    StrategicMemoryCorrectionBarrierEvent,
    StrategicMemoryEdge,
    StrategicMemoryItem,
    StrategicMemoryItemSource,
    StrategicMemoryItemVersion,
    StrategicMemoryProjectState,
    StrategicMemoryTransition,
)
from app.strategic_memory_retrieval import (
    RetrievalAccessPolicy,
    analyze_memory_query,
    retrieve_memory,
)
from app.strategic_memory_sources import SourceReceiptInput, parse_exact_spans, record_source, sha256_hex
from app.time_utils import _utcnow


async def _seed_item(
    db_session,
    *,
    project: Project,
    text: str,
    logical_key: str,
    status: str = "active",
    sensitivity: str = "normal",
):
    receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="task_result",
            source_key=f"source-{logical_key}-{uuid.uuid4()}",
            source_revision="1",
            content_hash=sha256_hex(text),
            occurred_at=_utcnow(),
            actor_kind="user",
            actor_id="operator",
            sensitivity=sensitivity,
            canonical_payload=text,
            mutable=True,
        ),
    )
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert state is not None
    state.incorporated_source_sequence = receipt.source.source_sequence
    item = StrategicMemoryItem(
        project_id=project.id,
        kind="notation",
        logical_key=logical_key,
        current_version=1,
        current_status=status,
    )
    spans = parse_exact_spans(text)
    formula = next((span for span in spans if span.kind.startswith("latex")), spans[0])
    version = StrategicMemoryItemVersion(
        item_id=item.id,
        version=1,
        project_id=project.id,
        canonical_text=text,
        structured_value={},
        epistemic_status="operator_explicit",
        confidence=1,
        recorded_source_sequence=receipt.source.source_sequence,
        exact_terms=[logical_key],
        formula_fingerprints=[form for form in formula.normalized_forms if form.startswith("formula-v1:")],
        content_hash=sha256_hex(text),
        extractor_version="test",
    )
    provenance = StrategicMemoryItemSource(
        item_id=item.id,
        item_version=1,
        source_id=receipt.source.id,
        source_role="assertion",
        span_index=0,
        project_id=project.id,
        span_kind="inline_math",
        span_start=formula.byte_start,
        span_end=formula.byte_end,
        span_hash=formula.span_hash,
    )
    db_session.add_all([item, version, state])
    await db_session.flush()
    # StrategicMemoryItemSource has a composite FK to ItemVersion. Keep this
    # fixture faithful to the database contract rather than relying on ORM
    # ordering for independent instances.
    db_session.add(provenance)
    await db_session.flush()
    return item, version, receipt.source


@pytest.mark.asyncio
async def test_exact_formula_retrieval_is_project_fenced_and_returns_verified_raw_span(db_session):
    project = Project(name="retrieval-a", user_id="retrieval-a")
    other = Project(name="retrieval-b", user_id="retrieval-b")
    db_session.add_all([project, other])
    await db_session.flush()
    await _seed_item(db_session, project=project, text="The value is $\\eta=0.2$.", logical_key="eta")
    await _seed_item(db_session, project=other, text="The value is $\\eta=99$.", logical_key="eta")

    result = await retrieve_memory(
        db_session,
        project_id=project.id,
        query="Give the exact formula $\\eta=0.2$.",
        access_policy=RetrievalAccessPolicy(include_raw_content=True),
    )

    assert result.answerable
    assert len(result.cards) == 1
    assert result.cards[0].raw_quote == "$\\eta=0.2$"
    assert result.cards[0].source_id is not None
    assert "exact" in result.cards[0].selection_reasons
    assert "99" not in result.cards[0].canonical_text


@pytest.mark.asyncio
async def test_current_retrieval_excludes_retracted_and_open_barrier_items(db_session):
    project = Project(name="retrieval-current", user_id="retrieval-current")
    db_session.add(project)
    await db_session.flush()
    active, _, active_source = await _seed_item(
        db_session, project=project, text="Use $x_i$ in the current proof.", logical_key="x_i"
    )
    retracted, retracted_version, retracted_source = await _seed_item(
        db_session, project=project, text="Use $x_j$ in an older proof.", logical_key="x_j", status="retracted"
    )
    # ``current_status`` is only a cache. Persist the immutable transition that
    # makes this item retracted at the queried source revision.
    db_session.add(
        StrategicMemoryTransition(
            project_id=project.id,
            item_id=retracted.id,
            item_version=retracted_version.version,
            transition="retract",
            source_id=retracted_source.id,
            recorded_source_sequence=retracted_source.source_sequence,
        )
    )
    barrier = StrategicMemoryCorrectionBarrier(
        project_id=project.id,
        correction_source_id=active_source.id,
        candidate_item_id=active.id,
        candidate_version=1,
        match_kind="exact",
        match_score=1,
        context_generation=0,
    )
    db_session.add(barrier)
    await db_session.flush()
    db_session.add(StrategicMemoryCorrectionBarrierEvent(
        project_id=project.id,
        barrier_id=barrier.id,
        event_type="opened",
        source_id=active_source.id,
        actor_kind="operator",
        actor_id="operator",
        reason="correction",
        recorded_source_sequence=active_source.source_sequence,
        context_generation=0,
    ))
    await db_session.flush()

    result = await retrieve_memory(db_session, project_id=project.id, query="What is the current x notation?")

    assert not result.cards
    assert result.abstain_reason == "earlier_discussion_not_found"
    historical = await retrieve_memory(
        db_session, project_id=project.id, query="What was the historical older x_j notation?"
    )
    assert [card.item_id for card in historical.cards] == [retracted.id]
    assert historical.cards[0].source_id == retracted_source.id


@pytest.mark.asyncio
async def test_raw_suffix_is_bounded_and_marks_degraded_without_memory_items(db_session):
    project = Project(name="retrieval-suffix", user_id="retrieval-suffix")
    db_session.add(project)
    await db_session.flush()
    for index in range(8):
        await record_source(
            db_session,
            SourceReceiptInput(
                project_id=project.id,
                source_type="task_result",
                source_key=f"suffix-{index}",
                source_revision="1",
                content_hash=sha256_hex(f"suffix {index}"),
                occurred_at=_utcnow(),
                actor_kind="user",
                actor_id="operator",
                canonical_payload=f"suffix {index}",
                mutable=True,
            ),
        )
    result = await retrieve_memory(
        db_session,
        project_id=project.id,
        query="fresh perspective",
        raw_suffix_limit=6,
        access_policy=RetrievalAccessPolicy(include_raw_content=True),
    )
    assert len(result.raw_suffix) == 6
    assert result.memory_degraded
    assert result.analysis.policy == "fresh_start"


@pytest.mark.asyncio
async def test_conflict_edge_expands_both_records_before_pack_pruning(db_session):
    project = Project(name="retrieval-conflict", user_id="retrieval-conflict")
    db_session.add(project)
    await db_session.flush()
    old, _, _ = await _seed_item(
        db_session, project=project, text="The experiment says $\\alpha=0.1$.", logical_key="alpha-old"
    )
    new, _, _ = await _seed_item(
        db_session, project=project, text="The experiment says $\\alpha=0.2$.", logical_key="alpha-new"
    )
    db_session.add(StrategicMemoryEdge(
        project_id=project.id,
        from_node_id=old.id,
        from_version=1,
        relation="contradicts",
        to_node_id=new.id,
        to_version=1,
        confidence=1,
    ))
    await db_session.flush()

    result = await retrieve_memory(
        db_session, project_id=project.id, query="Actually, which alpha claim conflicts?"
    )

    assert {card.item_id for card in result.cards} == {old.id, new.id}
    assert result.conflict_present
    assert any("contradicts" in path for card in result.cards for path in card.relation_path)


def test_query_analysis_distinguishes_history_fresh_start_and_formula_forms():
    historical = analyze_memory_query("As of last week, quote $x_i$ exactly")
    fresh = analyze_memory_query("Ignore our prior approach; fresh perspective")
    assert historical.policy == "strict_history" and historical.exact_required
    assert historical.formula_fingerprints
    assert fresh.policy == "fresh_start"


@pytest.mark.asyncio
async def test_as_of_status_reconstructs_active_before_retraction_and_retracted_after(db_session):
    project = Project(name="retrieval-history-status", user_id="retrieval-history-status")
    db_session.add(project)
    await db_session.flush()
    item, version, assertion_source = await _seed_item(
        db_session,
        project=project,
        text="The active notation is $x_i$.",
        logical_key="x_i",
    )
    retraction = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="task_result",
            source_key=f"retract-{uuid.uuid4()}",
            source_revision="1",
            content_hash=sha256_hex("retract x_i"),
            occurred_at=_utcnow(),
            actor_kind="operator",
            actor_id="operator",
            canonical_payload="retract x_i",
            mutable=True,
        ),
    )
    db_session.add(
        StrategicMemoryTransition(
            project_id=project.id,
            item_id=item.id,
            item_version=version.version,
            transition="retract",
            source_id=retraction.source.id,
            recorded_source_sequence=retraction.source.source_sequence,
        )
    )
    item.current_status = "retracted"  # present cache must not alter the past.
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert state is not None
    state.incorporated_source_sequence = retraction.source.source_sequence
    await db_session.flush()

    before = await retrieve_memory(
        db_session,
        project_id=project.id,
        query="What was the historical x_i notation?",
        source_sequence=assertion_source.source_sequence,
    )
    after = await retrieve_memory(
        db_session,
        project_id=project.id,
        query="What was the historical x_i notation?",
        source_sequence=retraction.source.source_sequence,
    )

    assert before.cards[0].status == "active"
    assert after.cards[0].status == "retracted"


@pytest.mark.asyncio
async def test_default_policy_excludes_restricted_provenance_and_all_raw_content(db_session):
    project = Project(name="retrieval-privacy", user_id="retrieval-privacy")
    db_session.add(project)
    await db_session.flush()
    await _seed_item(
        db_session,
        project=project,
        text="restricted source says $\\eta=9$.",
        logical_key="eta-restricted",
        sensitivity="restricted",
    )
    await _seed_item(
        db_session,
        project=project,
        text="normal source says $\\eta=2$.",
        logical_key="eta-normal",
    )

    result = await retrieve_memory(db_session, project_id=project.id, query="eta")

    assert [card.item_id for card in result.cards]
    assert all("restricted" not in card.canonical_text for card in result.cards)
    assert all(card.raw_quote is None and not card.raw_quote_available for card in result.cards)
    assert not result.raw_suffix
