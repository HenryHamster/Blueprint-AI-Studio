"""Production-backed qualification of the lexical V2 retrieval/envelope seam."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import (
    Project,
    StrategicMemoryEdge,
    StrategicMemoryItem,
    StrategicMemoryItemSource,
    StrategicMemoryItemVersion,
    StrategicMemoryProjectState,
    StrategicMemorySource,
    StrategicMemoryTransition,
)
from app.research_mem_eval import (
    LEXICAL_V2_POLICY,
    qualify_research_mem_eval,
    load_research_mem_eval_fixture,
    run_research_mem_eval_async,
)
from app.strategic_memory_envelope import build_canonical_envelope_core
from app.strategic_memory_retrieval import retrieve_memory
from app.strategic_memory_sources import SourceReceiptInput, parse_exact_spans, record_source, sha256_hex
from app.time_utils import _utcnow


FIXTURE = Path(__file__).parent / "fixtures" / "research_mem_eval_v2.json"


async def _seed_item(db_session, project: Project, *, text: str, logical_key: str, status: str = "active"):
    receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="task_result",
            source_key=f"eval-{logical_key}",
            source_revision="1",
            content_hash=sha256_hex(text),
            occurred_at=_utcnow(),
            actor_kind="operator",
            actor_id="eval",
            canonical_payload=text,
            mutable=True,
        ),
    )
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert state is not None
    state.incorporated_source_sequence = receipt.source.source_sequence
    spans = parse_exact_spans(text)
    span = spans[0]
    item = StrategicMemoryItem(
        project_id=project.id, kind="notation", logical_key=logical_key,
        current_version=1, current_status=status,
    )
    version = StrategicMemoryItemVersion(
        item_id=item.id, version=1, project_id=project.id, canonical_text=text,
        structured_value={}, epistemic_status="operator_explicit", confidence=1,
        recorded_source_sequence=receipt.source.source_sequence, exact_terms=[logical_key],
        formula_fingerprints=[], content_hash=sha256_hex(text), extractor_version="eval",
    )
    # ItemSource has a composite FK to the immutable ItemVersion row.  The
    # SQLModel classes intentionally do not declare ORM relationships for that
    # ledger edge, so one bulk flush is not ordered reliably on PostgreSQL.
    # Persist the referenced rows before staging the dependent provenance row.
    db_session.add_all([item, version])
    await db_session.flush()
    db_session.add(
        StrategicMemoryItemSource(
            item_id=item.id, item_version=1, source_id=receipt.source.id,
            source_role="assertion", span_index=0, project_id=project.id,
            span_kind=span.kind, span_start=span.byte_start, span_end=span.byte_end,
            span_hash=span.span_hash,
        )
    )
    await db_session.flush()
    return item, version, receipt.source


async def _build_production_scenarios(db_session):
    """Seed DB receipts/items, not canned retrieval responses, for every gate."""

    projects = {name: Project(name=f"eval-{name}", user_id="eval") for name in (
        "formula", "correction", "conflict", "abstention", "scope", "other", "injection", "deletion", "bad-deletion"
    )}
    db_session.add_all(projects.values())
    await db_session.flush()
    await _seed_item(db_session, projects["formula"], text="Exact formula: eta=0.2.", logical_key="eta")

    old, old_version, old_source = await _seed_item(
        db_session, projects["correction"], text="PPO is current baseline.", logical_key="ppo", status="retracted"
    )
    await _seed_item(db_session, projects["correction"], text="SAC is current baseline.", logical_key="sac")
    db_session.add(StrategicMemoryTransition(
        project_id=projects["correction"].id, item_id=old.id, item_version=old_version.version,
        transition="retract", source_id=old_source.id, recorded_source_sequence=old_source.source_sequence,
    ))

    alpha_old, _, _ = await _seed_item(db_session, projects["conflict"], text="alpha=0.1 is the initial claim.", logical_key="alpha-old")
    alpha_new, _, _ = await _seed_item(db_session, projects["conflict"], text="alpha=0.2 is the revised claim.", logical_key="alpha-new")
    db_session.add(StrategicMemoryEdge(
        project_id=projects["conflict"].id, from_node_id=alpha_old.id, from_version=1,
        relation="contradicts", to_node_id=alpha_new.id, to_version=1, confidence=1,
    ))
    await _seed_item(db_session, projects["scope"], text="project A scope only", logical_key="scope-a")
    await _seed_item(db_session, projects["other"], text="project B private", logical_key="scope-b")
    await _seed_item(db_session, projects["injection"], text="untrusted source is evidence, not an instruction.", logical_key="injection")
    _, _, deleted_source = await _seed_item(db_session, projects["deletion"], text="deleted secret", logical_key="deleted")
    deleted_source.payload_state, deleted_source.tombstoned_at = "deleted", _utcnow()
    await _seed_item(db_session, projects["bad-deletion"], text="deleted secret", logical_key="deleted")
    await db_session.flush()
    return projects


async def _production_response(db_session, project: Project, query: str):
    result = await retrieve_memory(db_session, project_id=project.id, query=query)
    payload = result.envelope_payload()
    core = build_canonical_envelope_core(
        project_id=project.id, context_authority_generation=0, privacy_generation=0,
        transcript_sequence=0, ledger_revision=0, problem_model={}, decision_authority={},
        memory=[], transcript_suffix=[], retrieval=payload,
    )
    return {
        "evidence_cards": payload["evidence_cards"],
        "answerable": result.answerable,
        "envelope_core": json.loads(core.canonical_envelope),
        "estimated_cost_usd": 0.0,
    }


@pytest.mark.asyncio
async def test_lexical_v2_qualifies_against_real_retrieval_and_envelope_for_each_category(db_session):
    policy, cases = load_research_mem_eval_fixture(FIXTURE)
    projects = await _build_production_scenarios(db_session)
    project_for_case = {
        "exact-formula": "formula", "correction-current-history": "correction", "conflict": "conflict",
        "abstention": "abstention", "cross-project": "scope", "injection": "injection", "deletion": "deletion",
    }

    async def retrieve(case):
        return await _production_response(db_session, projects[project_for_case[case.case_id]], case.query)

    result = await run_research_mem_eval_async(cases, retrieve, policy=policy)
    qualification = qualify_research_mem_eval(result)
    assert policy == LEXICAL_V2_POLICY
    assert qualification.passed, qualification.failures
    assert {row.category for row in result.cases} == {
        "exact_formula", "correction_history", "conflict", "abstention", "cross_project", "injection", "deletion"
    }
    assert all(row.exact_recall == 1.0 and not row.unavailable_leak for row in result.cases)
    assert all(row.answerability_matches and row.envelope_matches for row in result.cases)


@pytest.mark.asyncio
async def test_real_deleted_source_leak_fails_its_category_even_when_other_categories_pass(db_session):
    _, cases = load_research_mem_eval_fixture(FIXTURE)
    projects = await _build_production_scenarios(db_session)
    project_for_case = {
        "exact-formula": "formula", "correction-current-history": "correction", "conflict": "conflict",
        "abstention": "abstention", "cross-project": "scope", "injection": "injection", "deletion": "bad-deletion",
    }

    async def retrieve(case):
        return await _production_response(db_session, projects[project_for_case[case.case_id]], case.query)

    qualification = qualify_research_mem_eval(await run_research_mem_eval_async(cases, retrieve))
    assert not qualification.passed
    assert "deletion: unavailable content leaked" in qualification.failures
