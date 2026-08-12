"""Tests for the knowledge-document store and the web_research task type.

Covers:
  * the ``web_research`` capability + payload registration
  * ``_handle_web_research_completion``: a passing forward-quality grade
    persists exactly one ``KnowledgeDocument`` and marks COMPLETED; a failing
    grade marks FAILED with the verdict recorded and persists no document
  * the list / search / get-by-id admin endpoints: project scoping + a
    search hit
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import func
from sqlmodel import select

from app.knowledge_completion import (
    _PENDING_DOCUMENT_KEY,
    _handle_design_completion,
    _handle_web_research_completion,
    _is_usable_citation,
    _parse_web_research_document,
)
from app.models import KnowledgeDocument, Task, TaskStatus
from app.time_utils import _utcnow as _utcnow_func
from app.schemas import AgentStatusPayload
from app.task_evaluators import (
    QUALITY_GRADER_SOURCE_KEY,
    QUALITY_GRADER_VERDICT_KEY,
)
from app.task_outcomes import (
    KIND_KNOWLEDGE_DOCUMENT,
    TaskOutcome,
    build_task_evaluation_summary,
    evaluate_task_outcome,
)
from testing.factories import make_membership, make_project, make_task


# ── capability + payload registration ───────────────────────────────────────


def test_web_research_capability_registered():
    """web_research is a read-only, web-requiring, forward-quality knowledge type."""
    from app.task_capabilities import (
        COMPLETION_HANDLER_BY_TASK_TYPE,
        EVALUATION_STRATEGY_BY_TASK_TYPE,
        READ_ONLY_TASK_TYPES,
        TaskType,
        get_task_capability,
    )

    cap = get_task_capability(TaskType.web_research)
    assert cap is not None
    assert cap.label == "Web Research"
    assert cap.agent_mode == "read_only"
    assert cap.output_kind == "knowledge"
    assert cap.requires_web is True
    assert cap.requires_code is False
    assert cap.evaluation_strategy == "forward_quality"
    assert cap.user_launchable is True
    # The derived maps must pick up the declared handler + evaluation key.
    assert COMPLETION_HANDLER_BY_TASK_TYPE["web_research"] == "web_research"
    assert EVALUATION_STRATEGY_BY_TASK_TYPE["web_research"] == "forward-quality"
    assert "web_research" in READ_ONLY_TASK_TYPES


def test_web_research_payload_validates():
    """WebResearchPayload accepts the prompt + optional routing fields."""
    from app.payload_schemas import validate_task_payload

    raw = {
        "type": "web_research",
        "prompt": "Research the current state of X across the web.",
        "topic": "X",
        "tags": ["web", "survey"],
        "direction_id": "d-abc",
        "project_name": "Demo",
        "base_branch": "main",
    }
    out = validate_task_payload(raw)
    assert out["type"] == "web_research"
    assert out["topic"] == "X"
    assert out["tags"] == ["web", "survey"]


# ── _parse_web_research_document ────────────────────────────────────────────


class TestParseDocument:
    def test_bare_json_object(self):
        doc = {
            "title": "T",
            "summary": "S",
            "body": "# Body",
            "citations": ["https://example.com"],
            "tags": ["a"],
            "topic": "x",
        }
        out = _parse_web_research_document(json.dumps(doc))
        assert out is not None
        assert out["title"] == "T"

    def test_object_with_surrounding_text(self):
        doc = {"title": "T", "summary": "S", "body": "B"}
        text = "Here is the document:\n" + json.dumps(doc) + "\nDone."
        out = _parse_web_research_document(text)
        assert out is not None
        assert out["body"] == "B"

    def test_missing_required_key_returns_none(self):
        # No 'body' key.
        assert _parse_web_research_document('{"title": "T", "summary": "S"}') is None

    def test_empty_returns_none(self):
        assert _parse_web_research_document("") is None
        assert _parse_web_research_document(None) is None  # type: ignore[arg-type]


# ── _is_usable_citation predicate ───────────────────────────────────────────


class TestUsableCitation:
    def test_accepts_web_urls(self):
        assert _is_usable_citation("https://example.com/x") is True
        assert _is_usable_citation("http://example.com") is True

    def test_accepts_file_references(self):
        assert _is_usable_citation("file:docs/x.md") is True
        assert _is_usable_citation("docs/notes/source.md") is True

    def test_rejects_empty_and_whitespace(self):
        assert _is_usable_citation("") is False
        assert _is_usable_citation("   ") is False

    def test_rejects_non_string_and_junk(self):
        assert _is_usable_citation(None) is False  # type: ignore[arg-type]
        assert _is_usable_citation(123) is False  # type: ignore[arg-type]
        assert _is_usable_citation("not-a-url") is False


# ── completion handler (integration with db_session) ────────────────────────


@pytest.fixture
def _quiet_broadcast(mocker):
    mocker.patch(
        "app.knowledge_completion.broadcast_to_project",
        new=AsyncMock(return_value=None),
    )


async def _make_web_research_task(db_session, *, project_name="WR", context=None):
    project = make_project(name=project_name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Web Research: X",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "web_research", "topic": "X", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    if context is not None:
        task.context = context
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


@pytest.mark.asyncio
async def test_passing_grade_persists_one_document_and_completes(
    db_session, _quiet_broadcast
):
    """A well-formed document clears the forward-quality gate, is persisted as
    exactly one KnowledgeDocument, and the task is marked COMPLETED."""
    project, task = await _make_web_research_task(db_session)

    document = {
        "title": "State of X on the web",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body explaining X in depth.",
        "citations": ["https://example.com/x", "file:docs/x.md"],
        "tags": ["survey", "web"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    doc = docs[0]
    assert doc.project_id == project.id
    assert doc.title == document["title"]
    assert doc.body == document["body"]
    assert doc.tags == ["survey", "web"]
    assert doc.source_citations == document["citations"]
    assert doc.topic == "X"
    # Verdict captured from a passing gate.
    assert doc.forward_quality_verdict["passed"] is True
    assert doc.forward_quality_verdict["strategy"] == "forward-quality"

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # Compact mirror in plan_output (no body).
    assert isinstance(task.plan_output, dict)
    mirror = task.plan_output["knowledge_document"]
    assert mirror["title"] == document["title"]
    assert "body" not in mirror
    # The document id + forward-quality verdict are carried forward so the task
    # detail view can link to the produced document and show its gate verdict.
    assert mirror["id"] == str(doc.id)
    assert mirror["forward_quality_verdict"]["passed"] is True


@pytest.mark.asyncio
async def test_blank_title_document_still_records_successful_outcome(
    db_session, _quiet_broadcast
):
    project, task = await _make_web_research_task(db_session, project_name="WRblank")

    document = {
        "title": "",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body explaining X in depth.",
        "citations": ["https://example.com/x"],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    assert docs[0].project_id == project.id
    assert docs[0].title == ""

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert evaluate_task_outcome(task) == TaskOutcome(
        "success", KIND_KNOWLEDGE_DOCUMENT
    )
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "recorded"
    assert {"label": "Summary", "value": document["summary"]} in summary["evidence"]
    assert {"label": "Citations", "value": "1"} in summary["evidence"]


@pytest.mark.asyncio
async def test_failing_grade_marks_failed_and_persists_no_document(
    db_session, _quiet_broadcast
):
    """An empty terminal artifact fails the forward-quality gate: the task is
    marked FAILED with the verdict recorded, and NO document is persisted."""
    project, task = await _make_web_research_task(db_session, project_name="WRfail")

    await _handle_web_research_completion(task, "   ", db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "forward-quality"


@pytest.mark.asyncio
async def test_unparseable_document_fails_gate(db_session, _quiet_broadcast):
    """Non-empty output that is not a parseable document (passes the cheap
    coherence check but has no title/summary/body) must NOT complete clean —
    it is marked FAILED and persists no document."""
    project, task = await _make_web_research_task(db_session, project_name="WRjunk")

    junk = "This is a long enough prose blob but it is not a JSON document at all."
    await _handle_web_research_completion(task, junk, db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_no_citations_fails_gate_and_persists_no_document(
    db_session, _quiet_broadcast
):
    """A well-formed title/summary/body with an EMPTY citation list fails the
    forward-quality gate: web research must be grounded. No document persists."""
    project, task = await _make_web_research_task(db_session, project_name="WRnocite")

    document = {
        "title": "Ungrounded summary of X",
        "summary": "Looks plausible but cites nothing.",
        "body": "# X\n\nA detailed-looking body with no sources whatsoever.",
        "citations": [],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "forward-quality"


@pytest.mark.asyncio
async def test_junk_citations_fail_gate(db_session, _quiet_broadcast):
    """Citations that are empty / whitespace / non-URL junk are not usable: the
    gate fails and no document persists."""
    project, task = await _make_web_research_task(db_session, project_name="WRjunkcite")

    document = {
        "title": "Ungrounded summary of X",
        "summary": "Cites only junk.",
        "body": "# X\n\nA detailed-looking body with junk citations only.",
        "citations": ["", "   ", "not-a-url"],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_persist_document_idempotent_on_completion_replay(
    db_session, _quiet_broadcast
):
    """A second call to the completion handler (post-commit replay) on a web_research
    task that already persisted a document is idempotent: no duplicate is inserted.
    The task status transitions correctly only on first completion; replay finds the
    existing document and skips re-insertion."""
    project, task = await _make_web_research_task(
        db_session, project_name="WRidempotent"
    )

    document = {
        "title": "State of X on the web",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body explaining X in depth.",
        "citations": ["https://example.com/x", "file:docs/x.md"],
        "tags": ["survey", "web"],
        "topic": "X",
    }
    logs = json.dumps(document)

    # First completion: task starts IN_PROGRESS, completes, persists one document.
    await _handle_web_research_completion(task, logs, db_session)
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs_first = await _docs_for(db_session, task.id)
    assert len(docs_first) == 1
    first_doc_id = docs_first[0].id

    # Reset task to IN_PROGRESS to simulate post-commit recovery replay: the
    # handler failed to report status back to the orchestrator, so it re-runs
    # with the same output and context.
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    # Second completion (replay): the handler runs again with identical output.
    # This should NOT create a second document.
    await _handle_web_research_completion(task, logs, db_session)
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED  # Status transitions again.

    docs_replay = await _docs_for(db_session, task.id)
    # CRITICAL: still exactly one document (not two).
    assert len(docs_replay) == 1
    # The document is the same as before (same ID).
    assert docs_replay[0].id == first_doc_id


# ── goal-level acceptance probe (topical responsiveness) ────────────────────


async def _make_topic_task(db_session, topic, *, project_name="WRtopic"):
    project = make_project(name=project_name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(
        project_id=project.id,
        title="Web Research",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "web_research", "topic": topic, "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


async def _docs_for(db_session, task_id):
    return (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task_id
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_off_topic_document_fails_acceptance_probe(db_session, _quiet_broadcast):
    """A well-formed, cited document about an unrelated subject is a CLEAR topical
    miss for the asked topic: the goal-level probe fails the gate, no doc persists."""
    project, task = await _make_topic_task(
        db_session,
        "the current state of quantum error correction hardware",
        project_name="WRofftopic",
    )

    document = {
        "title": "A guide to organic vegetable gardening",
        "summary": "How to grow tomatoes, lettuce and carrots in a backyard plot.",
        "body": "# Gardening\n\nMulch, watering schedules, and companion planting.",
        "citations": ["https://example.com/gardening"],
        "tags": ["gardening"],
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    assert await _docs_for(db_session, task.id) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert "not responsive" in task.context["evaluation_gate"]["reason"]


@pytest.mark.asyncio
async def test_on_topic_document_passes_acceptance_probe(db_session, _quiet_broadcast):
    """A document genuinely responsive to the asked topic clears the probe and
    persists exactly one KnowledgeDocument."""
    project, task = await _make_topic_task(
        db_session,
        "the current state of quantum error correction hardware",
        project_name="WRontopic",
    )

    document = {
        "title": "State of quantum error correction hardware",
        "summary": "Survey of surface-code error correction on superconducting qubits.",
        "body": "# Quantum error correction\n\nHardware progress on logical qubits.",
        "citations": ["https://example.com/qec"],
        "tags": ["quantum"],
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = await _docs_for(db_session, task.id)
    assert len(docs) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_thin_topic_signal_errs_toward_acceptance(db_session, _quiet_broadcast):
    """When the requested topic carries too little signal (few content keywords)
    the probe cannot judge and accepts — uncertainty errs toward acceptance."""
    project, task = await _make_topic_task(db_session, "X", project_name="WRthin")

    document = {
        "title": "A guide to organic vegetable gardening",
        "summary": "How to grow tomatoes in a backyard plot.",
        "body": "# Gardening\n\nMulch and watering schedules.",
        "citations": ["https://example.com/gardening"],
        "tags": ["gardening"],
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    # Topic 'X' yields no content keywords → probe accepts despite mismatch.
    assert len(await _docs_for(db_session, task.id)) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_reference_citation_is_usable(db_session, _quiet_broadcast):
    """A file reference (no http) is a usable citation: the gate passes and a
    single document persists."""
    project, task = await _make_web_research_task(db_session, project_name="WRfilecite")

    document = {
        "title": "Grounded by a file reference",
        "summary": "Cites a file, not a URL.",
        "body": "# X\n\nDetailed markdown grounded in a file reference.",
        "citations": ["file:docs/x.md"],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    assert docs[0].source_citations == ["file:docs/x.md"]
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


# ── source_kind discriminator on the producing completion handlers ──────────


async def _make_design_task(db_session, *, project_name="DSGN"):
    project = make_project(name=project_name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Design: X",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "design", "topic": "X", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


@pytest.mark.asyncio
async def test_web_research_completion_sets_source_kind(db_session, _quiet_broadcast):
    """A persisted web_research document records source_kind == 'web_research'."""
    project, task = await _make_web_research_task(db_session, project_name="WRkind")

    document = {
        "title": "State of X on the web",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body explaining X in depth.",
        "citations": ["https://example.com/x"],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.source_kind == "web_research"


@pytest.mark.asyncio
async def test_design_completion_sets_source_kind(db_session, _quiet_broadcast):
    """A persisted design document records source_kind == 'design'."""
    project, task = await _make_design_task(db_session, project_name="DSGNkind")

    document = {
        "title": "Design for X subsystem",
        "summary": "A codebase-internal design grounded in file references.",
        "body": "# X\n\nDetailed design rationale referencing the code.",
        "citations": ["backend/app/models.py", "docs/design/x.md"],
        "tags": ["design"],
        "topic": "X",
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.source_kind == "design"
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_compute_experiment_persist_sets_source_kind(
    db_session, _quiet_broadcast
):
    """A persisted compute_experiment verdict records source_kind ==
    'compute_experiment' and synthesizes its document from the measured verdict
    (no agent-emitted title/summary/body), sharing the same durable store."""
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = make_project(name="CErecord")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="compute_experiment: measure X",
        status=TaskStatus.COMPLETED,
        payloads=[
            {"type": "compute_experiment", "experiment": "X→Y", "base_branch": "main"}
        ],
        context={"direction_id": "d-xyz"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    doc = await persist_compute_experiment_knowledge(
        task,
        outcome="SUCCESS",
        cost=0.42,
        criterion={
            "metric": "accuracy",
            "operator": ">=",
            "threshold": 0.9,
            "value": 0.93,
            "met": True,
            "measurable": True,
        },
        result_reference="https://storage.example.com/jobs/abc/result.json",
        session=db_session,
    )

    assert doc is not None
    assert doc.source_kind == "compute_experiment"
    assert doc.direction_id == "d-xyz"
    # https:// result_reference is not a durable citation (_is_durable_citation
    # rejects transient http(s):// URLs from the fallback document).
    assert doc.source_citations == []
    assert "accuracy" in doc.body


# ── compute-experiment citation grounding ────────────────────────────────────
#
# Compute-experiment knowledge documents now carry a citation-grounding stamp
# in their persisted verdict, mirroring the design/web_research lanes.
# The stamp records how many citations were checked vs. resolved to real durable
# artifacts, and a document whose citations are ALL unresolved is flagged
# citation_grounding_miss=True (additive/best-effort — never blocks completion).


async def _make_compute_task(db_session, *, project_name="CE", direction_id=None):
    project = make_project(name=project_name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    ctx = {}
    if direction_id:
        ctx["direction_id"] = direction_id

    task = make_task(
        project_id=project.id,
        title="compute_experiment: benchmark Y",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "experiment": "benchmark-Y",
                "base_branch": "main",
            }
        ],
        context=ctx or None,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


@pytest.mark.asyncio
async def test_compute_knowledge_unresolved_citation_is_flagged_ungrounded(
    db_session, _quiet_broadcast
):
    """When a compute document's result_reference has no backing Artifact row,
    the persisted verdict carries citation_grounding_miss=True and
    probes.grounding.grounded==0. The document is still persisted (best-effort)."""
    from app.knowledge_completion import persist_compute_experiment_knowledge

    _project, task = await _make_compute_task(db_session, project_name="CEungrounded")

    # No logs → no ComputeJob → result_reference has no artifact backing.
    doc = await persist_compute_experiment_knowledge(
        task,
        outcome="SUCCESS",
        cost=0.10,
        criterion=None,
        result_reference="gs://bucket/jobs/no-artifact/result.json",
        session=db_session,
        logs=None,
    )

    assert doc is not None
    assert doc.source_kind == "compute_experiment"
    verdict = doc.forward_quality_verdict
    # The document persists — grounding miss is flagged, not a hard failure.
    assert verdict.get("citation_grounding_miss") is True
    grounding = verdict["probes"]["grounding"]
    assert grounding["file_citations"] == 1
    assert grounding["grounded"] == 0
    assert "gs://bucket/jobs/no-artifact/result.json" in grounding["unresolved"]
    # An unresolved gs:// citation has no artifact backing — it is stripped from
    # source_citations so the document is not treated as acceptance-grade evidence.
    assert doc.source_citations == []


@pytest.mark.asyncio
async def test_compute_fallback_stamps_grounding_when_artifact_resolves(
    db_session, _quiet_broadcast
):
    """When the fallback path finds a real Artifact via the ComputeJob, the persisted
    verdict records grounded==1 and citation_grounding_miss is absent."""
    import json as _json

    from app.knowledge_completion import persist_compute_experiment_knowledge
    from app.models import Artifact, ArtifactType, ComputeJob, ComputeJobStatus

    _project, task = await _make_compute_task(db_session, project_name="CEgrounded")

    # Create an Artifact row to represent the durable result.
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="gs://bucket/jobs/real-job/",
        logs=_json.dumps(
            {"artifact_uri": "gs://bucket/jobs/real-job/", "file_count": 3}
        ),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    # Create a ComputeJob that links to the artifact.
    job = ComputeJob(
        project_id=_project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=artifact.id,
        result_reference="gs://bucket/jobs/real-job/result.json",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = _json.dumps({"job_id": str(job.id), "outcome": "SUCCESS"})

    doc = await persist_compute_experiment_knowledge(
        task,
        outcome="SUCCESS",
        cost=0.20,
        criterion=None,
        result_reference="gs://bucket/jobs/real-job/result.json",
        session=db_session,
        logs=logs,
    )

    assert doc is not None
    verdict = doc.forward_quality_verdict
    # Artifact resolved → grounded=1, no miss flag.
    assert "citation_grounding_miss" not in verdict
    grounding = verdict["probes"]["grounding"]
    assert grounding["file_citations"] == 1
    assert grounding["grounded"] == 1
    assert grounding["unresolved"] == []
    # A resolved gs:// citation IS kept — durable evidence with artifact backing.
    assert "gs://bucket/jobs/real-job/result.json" in doc.source_citations


@pytest.mark.asyncio
async def test_compute_grounding_errs_toward_acceptance_on_missing_stamp(
    db_session, _quiet_broadcast
):
    """Compute documents without a result_reference persist cleanly with no
    citation_grounding_miss flag — older agents or jobs without an artifact URI
    are never blocked by the grounding check."""
    from app.knowledge_completion import persist_compute_experiment_knowledge

    _project, task = await _make_compute_task(db_session, project_name="CEnoreference")

    doc = await persist_compute_experiment_knowledge(
        task,
        outcome="FAILED",
        cost=0.05,
        criterion=None,
        result_reference=None,
        session=db_session,
    )

    assert doc is not None
    verdict = doc.forward_quality_verdict
    # No result_reference → 0 citations checked → miss gate never fires.
    assert "citation_grounding_miss" not in verdict


@pytest.mark.asyncio
async def test_compute_persist_attaches_grounding_probe_to_verdict(
    db_session, _quiet_broadcast
):
    """_persist_compute_knowledge_document attaches grounding probe detail from
    a _citation_grounding stamp on the document dict into the persisted verdict."""
    from app.knowledge_completion import _persist_compute_knowledge_document

    _project, task = await _make_compute_task(db_session, project_name="CEprobe")

    document = {
        "title": "Probe test",
        "summary": "Summary for probe test.",
        "body": "# Body",
        "topic": "probe",
        "tags": ["compute_experiment"],
        "citations": ["gs://bucket/job/result.json"],
        "verdict_record": {
            "strategy": "metric",
            "outcome": "SUCCESS",
            "cost_usd": 0.10,
            "criterion": None,
            "criterion_met": None,
        },
        "_citation_grounding": {
            "checked": True,
            "file_citations": 2,
            "grounded": 1,
            "unresolved": ["gs://bucket/job/missing.json"],
        },
    }

    doc = await _persist_compute_knowledge_document(
        task, document, "fallback", db_session
    )

    verdict = doc.forward_quality_verdict
    grounding = verdict["probes"]["grounding"]
    assert grounding["file_citations"] == 2
    assert grounding["grounded"] == 1
    assert grounding["unresolved"] == ["gs://bucket/job/missing.json"]
    # One citation grounded → miss should NOT fire.
    assert "citation_grounding_miss" not in verdict


# ── knowledge-prerequisite delivery to dependents ───────────────────────────
#
# When a plan commissions a web_research/design prerequisite and makes an
# implementation task depend on it, the produced document must be delivered to
# the dependent's launch context. Linkage is established at prerequisite
# completion (so it survives the dependent dispatching later): a compact pointer
# is stamped onto each QUEUED dependent's context["knowledge_prerequisites"].


async def _make_queued_dependent(db_session, *, project_id, depends_on):
    """Create a QUEUED implementation task that depends on a prerequisite task."""
    dep = make_task(
        project_id=project_id,
        title="feat: implement X (depends on prereq)",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "implement X", "base_branch": "main"}],
        depends_on=depends_on,
    )
    db_session.add(dep)
    await db_session.commit()
    await db_session.refresh(dep)
    return dep


@pytest.mark.asyncio
async def test_completion_delivers_pointer_to_queued_dependent(
    db_session, _quiet_broadcast
):
    """A web_research prerequisite's completion stamps its document pointer onto a
    QUEUED dependent that declared it in depends_on."""
    project, task = await _make_web_research_task(db_session, project_name="WRdeliver")
    dependent = await _make_queued_dependent(
        db_session, project_id=project.id, depends_on=[str(task.id)]
    )

    document = {
        "title": "State of X on the web",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body explaining X in depth.",
        "citations": ["https://example.com/x"],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )

    await db_session.refresh(dependent)
    prereqs = (dependent.context or {}).get("knowledge_prerequisites")
    assert isinstance(prereqs, list) and len(prereqs) == 1
    pointer = prereqs[0]
    assert pointer["doc_id"] == str(doc.id)
    assert pointer["source_task_id"] == str(task.id)
    assert pointer["source_kind"] == "web_research"
    assert pointer["title"] == document["title"]
    assert pointer["summary"] == document["summary"]
    # The dependent stays QUEUED — delivery never advances its lifecycle.
    assert dependent.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_completion_no_dependents_is_noop(db_session, _quiet_broadcast):
    """A prerequisite with no declared dependents completes normally and stamps
    nothing — and an unrelated QUEUED task is untouched."""
    project, task = await _make_web_research_task(db_session, project_name="WRnodep")
    unrelated = await _make_queued_dependent(
        db_session, project_id=project.id, depends_on=[str(uuid.uuid4())]
    )

    document = {
        "title": "State of X on the web",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body explaining X in depth.",
        "citations": ["https://example.com/x"],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    await db_session.refresh(unrelated)
    assert "knowledge_prerequisites" not in (unrelated.context or {})


@pytest.mark.asyncio
async def test_design_completion_also_delivers(db_session, _quiet_broadcast):
    """The delivery seam is shared: a design prerequisite delivers its pointer to a
    QUEUED dependent exactly as web_research does."""
    project, task = await _make_design_task(db_session, project_name="DSGNdeliver")
    dependent = await _make_queued_dependent(
        db_session, project_id=project.id, depends_on=[str(task.id)]
    )

    document = {
        "title": "Design for X subsystem",
        "summary": "A codebase-internal design grounded in file references.",
        "body": "# X\n\nDetailed design rationale referencing the code.",
        "citations": ["backend/app/models.py", "docs/design/x.md"],
        "tags": ["design"],
        "topic": "X",
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    await db_session.refresh(dependent)
    prereqs = (dependent.context or {}).get("knowledge_prerequisites")
    assert isinstance(prereqs, list) and len(prereqs) == 1
    assert prereqs[0]["doc_id"] == str(doc.id)
    assert prereqs[0]["source_kind"] == "design"


_CANONICAL_COMPUTE_RESULT_URI = object()


async def _make_compute_delivery_fixture(
    db_session,
    *,
    project_name: str,
    storage_uri: str | None | object = _CANONICAL_COMPUTE_RESULT_URI,
    create_artifact: bool = True,
    create_job: bool = True,
):
    """Create one grounded compute document and a direct queued consumer."""
    from app.models import Artifact, ArtifactType, ComputeJob, ComputeJobStatus

    project, producer = await _make_compute_task(
        db_session, project_name=project_name
    )
    dependent = await _make_queued_dependent(
        db_session,
        project_id=project.id,
        depends_on=[str(producer.id)],
    )

    job = None
    if create_job:
        job = ComputeJob(
            project_id=project.id,
            task_id=producer.id,
            backend="local",
            status=ComputeJobStatus.SUCCEEDED,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    artifact = None
    if create_artifact:
        if storage_uri is _CANONICAL_COMPUTE_RESULT_URI:
            storage_uri = (
                "gs://blueprint-agent-artifacts/"
                f"compute-jobs/{job.id if job is not None else uuid.uuid4()}/"
                "artifacts/transfers/result.tar.gz"
            )
        artifact = Artifact(
            task_id=producer.id,
            artifact_type=ArtifactType.COMPUTE_RESULT,
            storage_uri=storage_uri,
        )
        db_session.add(artifact)
        await db_session.commit()
        await db_session.refresh(artifact)
        if job is not None:
            job.artifact_id = artifact.id
            db_session.add(job)
            await db_session.commit()
            await db_session.refresh(job)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=producer.id,
        source_compute_job_id=job.id if job is not None else None,
        source_kind="compute_experiment",
        title="Grounded compute result",
        summary="The measured result and its durable result bundle.",
        body="# Result\n\nMeasured on the compute runner.",
        topic="compute-result",
        source_citations=["gs://bucket/compute/jobs/result.tar.gz"],
        forward_quality_verdict={
            "analysis_mode": "llm",
            "probes": {"grounding": {"grounded": 1}},
        },
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return project, producer, dependent, doc, job, artifact


@pytest.mark.asyncio
async def test_compute_knowledge_delivers_result_artifact_to_dependent(
    db_session, mocker
):
    """A compute knowledge prerequisite authorizes its exact result bundle."""
    from app.knowledge_completion import _deliver_knowledge_to_dependents

    mocker.patch(
        "app.reaper_sweeps.event_driven_unblock_on_knowledge",
        new_callable=AsyncMock,
    )
    _project, producer, dependent, doc, job, artifact = (
        await _make_compute_delivery_fixture(
            db_session, project_name="CEartifact-delivery"
        )
    )

    await _deliver_knowledge_to_dependents(
        producer,
        doc,
        {"title": doc.title, "summary": doc.summary},
        db_session,
    )

    await db_session.refresh(dependent)
    artifact_prereqs = (dependent.context or {}).get("artifact_prerequisites")
    assert isinstance(artifact_prereqs, list) and len(artifact_prereqs) == 1
    assert artifact_prereqs[0] == {
        "artifact_id": str(artifact.id),
        "logical_key": None,
        "artifact_type": "COMPUTE_RESULT",
        "source_task_id": str(producer.id),
        "source_compute_job_id": str(job.id),
        "storage_uri": artifact.storage_uri,
    }


@pytest.mark.asyncio
async def test_compute_artifact_delivery_is_deduped(db_session, mocker):
    """Re-entrant knowledge delivery never duplicates either pointer."""
    from app.knowledge_completion import _deliver_knowledge_to_dependents

    mocker.patch(
        "app.reaper_sweeps.event_driven_unblock_on_knowledge",
        new_callable=AsyncMock,
    )
    _project, producer, dependent, doc, _job, _artifact = (
        await _make_compute_delivery_fixture(
            db_session, project_name="CEartifact-dedupe"
        )
    )
    normalized = {"title": doc.title, "summary": doc.summary}

    await _deliver_knowledge_to_dependents(
        producer, doc, normalized, db_session
    )
    await _deliver_knowledge_to_dependents(
        producer, doc, normalized, db_session
    )

    await db_session.refresh(dependent)
    assert len(dependent.context["knowledge_prerequisites"]) == 1
    assert len(dependent.context["artifact_prerequisites"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("create_job", "create_artifact", "storage_uri"),
    [
        (False, False, None),
        (True, False, None),
        (True, True, None),
    ],
    ids=["missing-job", "missing-artifact", "missing-storage-uri"],
)
async def test_compute_knowledge_without_downloadable_result_delivers_only_knowledge(
    db_session,
    mocker,
    create_job,
    create_artifact,
    storage_uri,
):
    """Incomplete legacy linkage does not authorize a nonexistent result bundle."""
    from app.knowledge_completion import _deliver_knowledge_to_dependents

    mocker.patch(
        "app.reaper_sweeps.event_driven_unblock_on_knowledge",
        new_callable=AsyncMock,
    )
    _project, producer, dependent, doc, _job, _artifact = (
        await _make_compute_delivery_fixture(
            db_session,
            project_name=f"CEartifact-{create_job}-{create_artifact}",
            storage_uri=storage_uri,
            create_artifact=create_artifact,
            create_job=create_job,
        )
    )

    await _deliver_knowledge_to_dependents(
        producer,
        doc,
        {"title": doc.title, "summary": doc.summary},
        db_session,
    )

    await db_session.refresh(dependent)
    assert len(dependent.context["knowledge_prerequisites"]) == 1
    assert "artifact_prerequisites" not in dependent.context


@pytest.mark.asyncio
async def test_compute_knowledge_rejects_cross_project_artifact_link(
    db_session, mocker
):
    """A job cannot authorize an artifact owned by a task in another project."""
    from app.knowledge_completion import _deliver_knowledge_to_dependents
    from app.models import Artifact, ArtifactType

    mocker.patch(
        "app.reaper_sweeps.event_driven_unblock_on_knowledge",
        new_callable=AsyncMock,
    )
    project, producer, dependent, doc, job, _artifact = (
        await _make_compute_delivery_fixture(
            db_session,
            project_name="CEartifact-project-a",
            create_artifact=False,
        )
    )
    other_project, other_task = await _make_compute_task(
        db_session, project_name="CEartifact-project-b"
    )
    assert other_project.id != project.id
    foreign_artifact = Artifact(
        task_id=other_task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="gs://bucket/foreign/result.tar.gz",
    )
    db_session.add(foreign_artifact)
    await db_session.commit()
    await db_session.refresh(foreign_artifact)
    job.artifact_id = foreign_artifact.id
    db_session.add(job)
    await db_session.commit()

    await _deliver_knowledge_to_dependents(
        producer,
        doc,
        {"title": doc.title, "summary": doc.summary},
        db_session,
    )

    await db_session.refresh(dependent)
    assert len(dependent.context["knowledge_prerequisites"]) == 1
    assert "artifact_prerequisites" not in dependent.context


@pytest.mark.asyncio
async def test_compute_knowledge_rejects_mismatched_artifact_uri_job(
    db_session, mocker
):
    """A document cannot deliver a bundle stored beneath another job prefix."""
    from app.knowledge_completion import _deliver_knowledge_to_dependents

    mocker.patch(
        "app.reaper_sweeps.event_driven_unblock_on_knowledge",
        new_callable=AsyncMock,
    )
    _project, producer, dependent, doc, _job, _artifact = (
        await _make_compute_delivery_fixture(
            db_session,
            project_name="CEartifact-uri-job-mismatch",
            storage_uri=(
                "gs://blueprint-agent-artifacts/"
                f"compute-jobs/{uuid.uuid4()}/artifacts/transfers/result.tar.gz"
            ),
        )
    )

    await _deliver_knowledge_to_dependents(
        producer,
        doc,
        {"title": doc.title, "summary": doc.summary},
        db_session,
    )

    await db_session.refresh(dependent)
    assert len(dependent.context["knowledge_prerequisites"]) == 1
    assert "artifact_prerequisites" not in dependent.context


@pytest.mark.asyncio
async def test_late_compute_knowledge_dependent_backfills_canonical_artifact(
    db_session,
):
    """A child created after the compute document still gets its exact bundle."""
    from app.artifact_prerequisites import backfill_artifact_prerequisites

    project, producer, _early_dependent, _doc, job, artifact = (
        await _make_compute_delivery_fixture(
            db_session, project_name="CEartifact-late-child"
        )
    )
    producer.status = TaskStatus.COMPLETED
    db_session.add(producer)
    await db_session.commit()

    late_dependent = await _make_queued_dependent(
        db_session,
        project_id=project.id,
        depends_on=[str(producer.id)],
    )
    assert "artifact_prerequisites" not in (late_dependent.context or {})

    await backfill_artifact_prerequisites(late_dependent, db_session)
    await db_session.commit()
    await backfill_artifact_prerequisites(late_dependent, db_session)
    await db_session.commit()
    await db_session.refresh(late_dependent)

    pointers = late_dependent.context["artifact_prerequisites"]
    assert len(pointers) == 1
    assert pointers[0]["artifact_id"] == str(artifact.id)
    assert pointers[0]["source_compute_job_id"] == str(job.id)
    assert pointers[0]["logical_key"] is None


# ── design codebase-grounding probe (file citations resolve to real paths) ──
#
# A design document's value is that it is grounded in the ACTUAL code. The
# design agent resolves its file-reference citations against the cloned working
# tree and stamps the result under ``_citation_grounding``; the completion gate
# fails ONLY the clear case where file citations resolve to ZERO real paths,
# erring toward acceptance otherwise. web_research docs carry no such stamp and
# are unaffected.


@pytest.mark.asyncio
async def test_design_all_file_citations_resolve_passes(db_session, _quiet_broadcast):
    """A design doc whose file citations ALL resolve to real paths is grounded:
    the probe accepts and persists exactly one KnowledgeDocument."""
    project, task = await _make_design_task(db_session, project_name="DSGNgrounded")

    document = {
        "title": "Design for the X subsystem",
        "summary": "A codebase-internal design grounded in real file references.",
        "body": "# X\n\nGrounded design rationale referencing real modules.",
        "citations": ["backend/app/models.py", "file:backend/app/reaper.py"],
        "tags": ["design"],
        "topic": "X",
        "_citation_grounding": {"file_citations": 2, "grounded": 2, "checked": True},
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    assert len(await _docs_for(db_session, task.id)) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_design_ungrounded_citations_fail_with_distinct_reason(
    db_session, _quiet_broadcast
):
    """A design doc whose file citations resolve to NOTHING real is not
    codebase-internal: the grounding probe fails the gate with a reason distinct
    from the 'no usable citation' and 'not responsive' reasons; no doc persists."""
    project, task = await _make_design_task(db_session, project_name="DSGNungrounded")

    document = {
        "title": "Design for the X subsystem",
        "summary": "Looks grounded but cites only hallucinated paths.",
        "body": "# X\n\nDesign referencing files that do not exist in the repo.",
        "citations": ["backend/app/made_up.py", "file:agent/src/hallucinated.ts"],
        "tags": ["design"],
        "topic": "X",
        "_citation_grounding": {"file_citations": 2, "grounded": 0, "checked": True},
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    assert await _docs_for(db_session, task.id) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    reason = task.context["evaluation_gate"]["reason"]
    assert "not grounded in the codebase" in reason
    assert "no usable source citation" not in reason
    assert "not responsive" not in reason


@pytest.mark.asyncio
async def test_design_mix_of_real_and_proposed_paths_passes(
    db_session, _quiet_broadcast
):
    """A realistic design legitimately references not-yet-created files alongside
    real ones: a single grounded citation among proposed paths is enough — the
    probe errs toward acceptance and persists the document."""
    project, task = await _make_design_task(db_session, project_name="DSGNmix")

    document = {
        "title": "Design for the X subsystem",
        "summary": "Grounded in real modules, proposing a new one.",
        "body": "# X\n\nExtends backend/app/reaper.py with a new sweep module.",
        "citations": [
            "backend/app/reaper.py",
            "backend/app/new_sweep_module.py",
            "backend/app/another_proposed.py",
        ],
        "tags": ["design"],
        "topic": "X",
        "_citation_grounding": {"file_citations": 3, "grounded": 1, "checked": True},
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    assert len(await _docs_for(db_session, task.id)) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


# ── web_research web-citation grounding probe (cited URLs are reachable) ─────
#
# Symmetric to the design grounding probe above: a web_research document's value
# is that it is grounded in real, reachable web sources. The web-research agent
# probes each cited URL for reachability and stamps the SAME ``_citation_grounding``
# shape design uses (``file_citations`` = URLs checked, ``grounded`` = reachable,
# ``unresolved`` = dead URLs). The completion gate fails ONLY the clear case where
# every checked URL is unreachable, erring toward acceptance otherwise. The
# rejection reason is URL-appropriate, NOT the codebase/file wording.


@pytest.mark.asyncio
async def test_web_research_all_urls_dead_fails_with_url_reason(
    db_session, _quiet_broadcast
):
    """A web_research doc whose cited URLs are ALL unreachable is ungrounded: the
    probe fails the gate with a URL-appropriate reason (distinct from the design
    'not grounded in the codebase' wording) and persists no document."""
    project, task = await _make_topic_task(
        db_session,
        "the current state of quantum error correction hardware",
        project_name="WRdeadurls",
    )
    document = {
        "title": "State of quantum error correction hardware",
        "summary": "Survey of surface-code error correction on superconducting qubits.",
        "body": "# Quantum error correction\n\nHardware progress on logical qubits.",
        "citations": ["https://example.com/dead-one", "https://example.com/dead-two"],
        "tags": ["quantum"],
        "_citation_grounding": {"file_citations": 2, "grounded": 0, "checked": True},
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    assert await _docs_for(db_session, task.id) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    reason = task.context["evaluation_gate"]["reason"]
    assert "unreachable" in reason
    assert "not grounded in the codebase" not in reason
    assert "no usable source citation" not in reason
    assert "not responsive" not in reason


@pytest.mark.asyncio
async def test_web_research_one_url_reachable_passes(db_session, _quiet_broadcast):
    """At least one reachable URL among dead ones is enough — the probe errs
    toward acceptance and persists exactly one KnowledgeDocument."""
    project, task = await _make_topic_task(
        db_session,
        "the current state of quantum error correction hardware",
        project_name="WRonereach",
    )
    document = {
        "title": "State of quantum error correction hardware",
        "summary": "Survey of surface-code error correction on superconducting qubits.",
        "body": "# Quantum error correction\n\nHardware progress on logical qubits.",
        "citations": ["https://example.com/live", "https://example.com/dead"],
        "tags": ["quantum"],
        "_citation_grounding": {"file_citations": 2, "grounded": 1, "checked": True},
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    assert len(await _docs_for(db_session, task.id)) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_web_research_unchecked_grounding_stamp_passes(
    db_session, _quiet_broadcast
):
    """An unchecked grounding stamp (older agent / probe skipped) is ignored: the
    gate errs toward acceptance and persists the document untouched."""
    project, task = await _make_topic_task(
        db_session,
        "the current state of quantum error correction hardware",
        project_name="WRunchecked",
    )
    document = {
        "title": "State of quantum error correction hardware",
        "summary": "Survey of surface-code error correction on superconducting qubits.",
        "body": "# Quantum error correction\n\nHardware progress on logical qubits.",
        "citations": ["https://example.com/qec"],
        "tags": ["quantum"],
        # checked is falsy → stamp ignored even though grounded is 0.
        "_citation_grounding": {"file_citations": 1, "grounded": 0, "checked": False},
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    assert len(await _docs_for(db_session, task.id)) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_web_research_pass_persists_grounding_probe_detail(
    db_session, _quiet_broadcast
):
    """A passing web_research document carries grounding detail (counts + the
    SPECIFIC dead URL strings) in its persisted verdict, symmetric to design."""
    project, task = await _make_topic_task(
        db_session,
        "the current state of quantum error correction hardware",
        project_name="WRgrounddetail",
    )
    document = {
        "title": "State of quantum error correction hardware",
        "summary": "Survey of surface-code error correction on superconducting qubits.",
        "body": "# Quantum error correction\n\nHardware progress on logical qubits.",
        "citations": ["https://example.com/live", "https://example.com/dead"],
        "tags": ["quantum"],
        # Agent-stamped: 2 URLs checked, 1 reachable, 1 dead.
        "_citation_grounding": {
            "file_citations": 2,
            "grounded": 1,
            "checked": True,
            "unresolved": ["https://example.com/dead"],
        },
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = await _docs_for(db_session, task.id)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["passed"] is True
    grounding = verdict["probes"]["grounding"]
    assert grounding["file_citations"] == 2
    assert grounding["grounded"] == 1
    assert grounding["unresolved"] == ["https://example.com/dead"]


# ── acceptance-probe DETAIL persisted alongside the pass/fail verdict ────────
#
# The forward-quality gate stores a binary pass/fail, but operators also need to
# judge HOW trustworthy a passing document is. The persisted
# ``forward_quality_verdict`` blob now carries the structured probe detail under
# a ``probes`` key (purely additive — the existing passed/reason/strategy/
# grader_task_id keys are untouched and pass/fail outcomes are unchanged).


@pytest.mark.asyncio
async def test_design_pass_persists_grounding_and_topic_probe_detail(
    db_session, _quiet_broadcast
):
    """A passing design document carries grounding detail (counts + the SPECIFIC
    unresolved citation strings) and topic-responsiveness detail in its persisted
    verdict, while the legacy verdict keys remain intact."""
    project = make_project(name="DSGNprobe")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(
        project_id=project.id,
        title="Design: reaper sweep",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "design",
                "topic": "reaper blocked task sweep design",
                "base_branch": "main",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    document = {
        "title": "Design for the reaper blocked-task sweep",
        "summary": "A codebase-grounded sweep design referencing real modules.",
        "body": "# Reaper\n\nThe sweep over blocked tasks extends real modules.",
        "citations": [
            "backend/app/reaper.py",
            "backend/app/proposed_sweep.py",
        ],
        "tags": ["design"],
        "topic": "reaper blocked task sweep design",
        # Agent-stamped grounding: 2 file citations, 1 grounded, 1 unresolved.
        "_citation_grounding": {
            "file_citations": 2,
            "grounded": 1,
            "checked": True,
            "unresolved": ["backend/app/proposed_sweep.py"],
        },
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    docs = await _docs_for(db_session, task.id)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    # Legacy keys untouched.
    assert verdict["passed"] is True
    assert verdict["strategy"] == "forward-quality"
    assert verdict["grader_task_id"] is None
    # Grounding detail carries the counts AND the specific unresolved citation.
    grounding = verdict["probes"]["grounding"]
    assert grounding["file_citations"] == 2
    assert grounding["grounded"] == 1
    assert grounding["unresolved"] == ["backend/app/proposed_sweep.py"]
    # Topic-responsiveness detail: passed flag + overlap/total keyword counts.
    topic = verdict["probes"]["topic_responsiveness"]
    assert topic["passed"] is True
    assert topic["total"] >= 3
    assert topic["overlap"] >= 1
    assert topic["overlap"] <= topic["total"]


@pytest.mark.asyncio
async def test_web_research_pass_persists_topic_detail_without_grounding(
    db_session, _quiet_broadcast
):
    """A web_research document has no file-citation grounding stamp: the persisted
    probe detail carries topic-responsiveness but OMITS the grounding key."""
    project, task = await _make_topic_task(
        db_session,
        "the current state of quantum error correction hardware",
        project_name="WRprobe",
    )
    document = {
        "title": "State of quantum error correction hardware",
        "summary": "Survey of surface-code error correction on superconducting qubits.",
        "body": "# Quantum error correction\n\nHardware progress on logical qubits.",
        "citations": ["https://example.com/qec"],
        "tags": ["quantum"],
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = await _docs_for(db_session, task.id)
    assert len(docs) == 1
    probes = docs[0].forward_quality_verdict["probes"]
    assert "grounding" not in probes
    assert probes["topic_responsiveness"]["passed"] is True
    assert probes["topic_responsiveness"]["total"] >= 3


@pytest.mark.asyncio
async def test_off_topic_design_probe_detail_records_failed_topic_flag(
    db_session, _quiet_broadcast
):
    """The topic-responsiveness detail's ``passed`` flag agrees with the gate: an
    off-topic design still FAILS the gate (no doc persisted) — the detail is only
    persisted on a passing document, so this asserts the gate decision is
    unchanged by the additive probe-detail wiring."""
    project = make_project(name="DSGNofftopic")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(
        project_id=project.id,
        title="Design: reaper sweep",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "design",
                "topic": "reaper blocked task sweep scheduling design",
                "base_branch": "main",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    document = {
        "title": "A guide to organic vegetable gardening",
        "summary": "How to grow tomatoes, lettuce and carrots in a backyard plot.",
        "body": "# Gardening\n\nMulch, watering schedules, and companion planting.",
        "citations": ["backend/app/reaper.py"],
        "tags": ["gardening"],
        "topic": "gardening",
        "_citation_grounding": {
            "file_citations": 1,
            "grounded": 1,
            "checked": True,
            "unresolved": [],
        },
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    # Unchanged gate decision: a clear topical miss FAILS, no document persists.
    assert await _docs_for(db_session, task.id) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "not responsive" in task.context["evaluation_gate"]["reason"]


# ── forward-quality grader escalation + resume (flag ON) ────────────────────
#
# web_research's honest quality gate has two tiers: the cheap synchronous
# structural gate (always) and an optional INDEPENDENT forward-quality grader
# (research_quality_grader_enabled, dark by default) that re-enters the
# completion path with a pass/fail verdict. These tests prove the web_research
# slice of the escalation/resume path end-to-end, mirroring the research and
# security_audit grader-resume tests.


def _enable_web_research_grader(mocker):
    """Flip the dark-shipped research-quality-grader flag ON for one test."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"research_quality_grader_enabled": True},
    )


def _well_formed_document() -> dict:
    return {
        "title": "State of X on the web",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body explaining X in depth.",
        "citations": ["https://example.com/x", "file:docs/x.md"],
        "tags": ["survey", "web"],
        "topic": "X",
    }


@pytest.mark.asyncio
async def test_grader_enabled_escalates_and_parks(db_session, _quiet_broadcast, mocker):
    """With the grader flag ON, a valid cited document does NOT persist on the
    first pass: the task parks EVALUATING behind exactly one independent grader,
    the parsed document is stashed in context, and no KnowledgeDocument yet."""
    _enable_web_research_grader(mocker)
    project, task = await _make_web_research_task(db_session, project_name="WRgrade")

    document = _well_formed_document()
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    # No document persisted yet — it is gated behind the grade.
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    # The parsed document is stashed so the resume pass can persist it intact.
    assert task.context[_PENDING_DOCUMENT_KEY]["body"] == document["body"]

    # Exactly one independent grader task, linked back to the source.
    graders = (
        (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id, Task.id != task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(graders) == 1
    grader = graders[0]
    assert (grader.context or {}).get(QUALITY_GRADER_SOURCE_KEY) == str(task.id)
    assert (grader.first_payload or {}).get("type") == "investigate"


@pytest.mark.asyncio
async def test_grader_pass_persists_one_doc_and_records_verdict(
    db_session, _quiet_broadcast
):
    """RESUME pass with a PASS verdict pre-seeded in context persists exactly one
    document, records the grader verdict (including grader_task_id), COMPLETED."""
    project, task = await _make_web_research_task(db_session, project_name="WRpass")

    document = _well_formed_document()
    grader_id = str(uuid.uuid4())
    ctx = dict(task.context or {})
    ctx[_PENDING_DOCUMENT_KEY] = document
    ctx[QUALITY_GRADER_VERDICT_KEY] = {
        "passed": True,
        "reason": "well-grounded survey",
        "grader_task_id": grader_id,
        "strategy": "forward-quality",
    }
    task.context = ctx
    db_session.add(task)
    await db_session.commit()

    # On resume the reconstructed logs are only the status line; persistence must
    # rely on the stashed document, so pass an empty body here.
    await _handle_web_research_completion(task, "", db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    doc = docs[0]
    assert doc.body == document["body"]
    assert doc.forward_quality_verdict["passed"] is True
    assert doc.forward_quality_verdict["grader_task_id"] == grader_id
    # Probe detail rides the grader-resume persist path too (additive, alongside
    # the grader verdict's own keys). This web_research doc has no grounding stamp.
    probes = doc.forward_quality_verdict["probes"]
    assert "grounding" not in probes
    assert probes["topic_responsiveness"]["passed"] is True

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_grader_fail_marks_failed_and_persists_no_document(
    db_session, _quiet_broadcast
):
    """RESUME pass with a FAIL verdict marks the source FAILED and persists no
    document — a failing independent grade never completes clean."""
    project, task = await _make_web_research_task(
        db_session, project_name="WRfailgrade"
    )

    document = _well_formed_document()
    ctx = dict(task.context or {})
    ctx[_PENDING_DOCUMENT_KEY] = document
    ctx[QUALITY_GRADER_VERDICT_KEY] = {
        "passed": False,
        "reason": "claims unsupported by the cited sources",
        "grader_task_id": str(uuid.uuid4()),
        "strategy": "forward-quality",
    }
    task.context = ctx
    db_session.add(task)
    await db_session.commit()

    await _handle_web_research_completion(task, "", db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "forward-quality"


@pytest.mark.asyncio
async def test_grader_resume_reconstructs_document_from_stash_body_intact(
    db_session, _quiet_broadcast, mocker
):
    """Full e2e through ``_resolve_quality_grader_completion``: a parked source
    whose plan_output is unset (so the reconstructed resume logs carry only the
    status line) must recover the parsed document from the context stash and
    persist its body INTACT. This guards the subtle stash-recovery the resume
    path depends on."""
    from app.task_lifecycle import _resolve_quality_grader_completion

    # The resolver broadcasts from task_lifecycle; quiet that too.
    mocker.patch(
        "app.task_lifecycle.broadcast_to_project", new=AsyncMock(return_value=None)
    )

    project, source = await _make_web_research_task(db_session, project_name="WRresume")

    document = _well_formed_document()
    # Park the source exactly as spawn_forward_quality_grader leaves it: stashed
    # document + awaiting flag, EVALUATING, and crucially NO plan_output (so the
    # resolver's resume_logs reconstruction falls to the status line, not a body).
    source.status = TaskStatus.EVALUATING
    source.plan_output = None
    ctx = dict(source.context or {})
    ctx[_PENDING_DOCUMENT_KEY] = document
    ctx["awaiting_quality_grader"] = True
    source.context = ctx
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    grader = make_task(
        project_id=project.id,
        title="[GRADE] forward-quality grade for web_research",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate", "base_branch": "main"}],
        context={QUALITY_GRADER_SOURCE_KEY: str(source.id)},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_by="ORCHESTRATOR",
    )
    source.context = {
        **(source.context or {}),
        "awaiting_quality_grader": str(grader.id),
    }
    db_session.add(source)
    db_session.add(grader)
    await db_session.commit()
    await db_session.refresh(grader)

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='{"verdict": "pass", "reasoning": "well-grounded and cited"}',
    )
    await _resolve_quality_grader_completion(grader, payload, db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == source.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    # The persisted body must equal the ORIGINAL stashed body — proving the
    # resume path recovered the document from the stash, not from the (status
    # line only) reconstructed logs.
    assert docs[0].body == document["body"]
    assert docs[0].source_citations == document["citations"]

    await db_session.refresh(source)
    assert source.status == TaskStatus.COMPLETED
    assert source.context[QUALITY_GRADER_VERDICT_KEY]["passed"] is True


@pytest.mark.asyncio
async def test_direction_id_persisted_from_task_context(db_session, _quiet_broadcast):
    """A direction-tagged web_research task records its direction on the
    persisted document and mirrors it into plan_output."""
    _project, task = await _make_web_research_task(
        db_session, project_name="WRdir", context={"direction_id": "d-abc123"}
    )

    document = {
        "title": "Direction-grounded survey",
        "summary": "S",
        "body": "# Body",
        "topic": "X",
        "citations": ["https://example.com/x"],
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.direction_id == "d-abc123"
    await db_session.refresh(task)
    assert task.plan_output["knowledge_document"]["direction_id"] == "d-abc123"


@pytest.mark.asyncio
async def test_direction_id_null_when_context_lacks_it(db_session, _quiet_broadcast):
    """A web_research task with no direction in context persists a null direction."""
    project, task = await _make_web_research_task(db_session, project_name="WRnodir")

    document = {
        "title": "T",
        "summary": "S",
        "body": "# Body",
        "citations": ["https://example.com/x"],
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.direction_id is None


# ── list / search / get-by-id endpoints ─────────────────────────────────────


async def _seed_project_with_membership(db_session, name: str):
    project = make_project(name=name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id, user_id="test-user-123"))
    await db_session.commit()
    return project


async def _seed_doc(db_session, project_id, source_task_id, **kwargs):
    defaults = dict(
        title="Doc title",
        summary="Doc summary",
        body="Doc body markdown",
        topic="alpha",
        tags=["t1"],
        source_citations=["https://example.com"],
        source_kind="web_research",
        forward_quality_verdict={"passed": True, "strategy": "forward-quality"},
    )
    defaults.update(kwargs)
    doc = KnowledgeDocument(
        project_id=project_id, source_task_id=source_task_id, **defaults
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


@pytest.mark.asyncio
async def test_list_is_project_scoped(client: AsyncClient, db_session):
    """Listing project A returns only A's documents, never project B's."""
    proj_a = await _seed_project_with_membership(db_session, "KDProjA")
    proj_b = await _seed_project_with_membership(db_session, "KDProjB")

    task_a = make_task(project_id=proj_a.id, status=TaskStatus.COMPLETED)
    task_b = make_task(project_id=proj_b.id, status=TaskStatus.COMPLETED)
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    await _seed_doc(db_session, proj_a.id, task_a.id, title="Alpha doc in A")
    await _seed_doc(db_session, proj_b.id, task_b.id, title="Beta doc in B")

    resp = await client.get(f"/api/admin/projects/{proj_a.id}/knowledge-documents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["documents"]) == 1
    assert body["documents"][0]["title"] == "Alpha doc in A"
    # List shape omits the markdown body.
    assert "body" not in body["documents"][0]


@pytest.mark.asyncio
async def test_search_hits_title_and_body(client: AsyncClient, db_session):
    """The q search term matches on title/body; non-matching docs are excluded."""
    proj = await _seed_project_with_membership(db_session, "KDSearch")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _seed_doc(
        db_session, proj.id, task.id, title="Quantum computing survey", body="qubits"
    )
    await _seed_doc(
        db_session, proj.id, task.id, title="Gardening basics", body="soil and water"
    )

    resp = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={"q": "quantum"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["documents"][0]["title"] == "Quantum computing survey"


@pytest.mark.asyncio
async def test_get_by_id_returns_body_and_scopes(client: AsyncClient, db_session):
    """get-by-id returns the full document (with body); a doc from another
    project under this project's path is a 404."""
    proj_a = await _seed_project_with_membership(db_session, "KDGetA")
    proj_b = await _seed_project_with_membership(db_session, "KDGetB")
    task_a = make_task(project_id=proj_a.id, status=TaskStatus.COMPLETED)
    task_b = make_task(project_id=proj_b.id, status=TaskStatus.COMPLETED)
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    doc_a = await _seed_doc(db_session, proj_a.id, task_a.id, body="full body A")
    doc_b = await _seed_doc(db_session, proj_b.id, task_b.id)

    ok = await client.get(
        f"/api/admin/projects/{proj_a.id}/knowledge-documents/{doc_a.id}"
    )
    assert ok.status_code == 200
    assert ok.json()["body"] == "full body A"

    # doc_b belongs to project B — not reachable under project A's path.
    cross = await client.get(
        f"/api/admin/projects/{proj_a.id}/knowledge-documents/{doc_b.id}"
    )
    assert cross.status_code == 404


@pytest.mark.asyncio
async def test_direction_filter_and_serialization(client: AsyncClient, db_session):
    """?direction= filters to one direction; the list shape exposes direction_id."""
    proj = await _seed_project_with_membership(db_session, "KDdir")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _seed_doc(
        db_session, proj.id, task.id, title="In dir one", direction_id="d-one"
    )
    await _seed_doc(
        db_session, proj.id, task.id, title="In dir two", direction_id="d-two"
    )

    resp = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={"direction": "d-one"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["documents"][0]["title"] == "In dir one"
    assert body["documents"][0]["direction_id"] == "d-one"


@pytest.mark.asyncio
async def test_list_and_get_serialize_source_kind(client: AsyncClient, db_session):
    """Both the list and get-by-id endpoints return the source_kind field."""
    proj = await _seed_project_with_membership(db_session, "KDkindserial")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    doc = await _seed_doc(
        db_session, proj.id, task.id, title="Design doc", source_kind="design"
    )

    listed = await client.get(f"/api/admin/projects/{proj.id}/knowledge-documents")
    assert listed.status_code == 200
    docs = listed.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["source_kind"] == "design"

    detail = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents/{doc.id}"
    )
    assert detail.status_code == 200
    assert detail.json()["source_kind"] == "design"


@pytest.mark.asyncio
async def test_list_filters_by_source_kind(client: AsyncClient, db_session):
    """The source_kind list filter scopes results to a single producing kind."""
    proj = await _seed_project_with_membership(db_session, "KDkindfilter")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _seed_doc(
        db_session, proj.id, task.id, title="A design doc", source_kind="design"
    )
    await _seed_doc(
        db_session,
        proj.id,
        task.id,
        title="A research doc",
        source_kind="web_research",
    )

    resp = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={"source_kind": "design"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["documents"]) == 1
    assert body["documents"][0]["title"] == "A design doc"
    assert body["documents"][0]["source_kind"] == "design"


# ── provenance (authoring_branch / head_sha) ─────────────────────────────────


@pytest.mark.asyncio
async def test_normalize_document_extracts_provenance(db_session):
    """_normalize_document extracts authoring_branch and head_sha from the doc."""
    from app.knowledge_completion import _normalize_document

    project, task = await _make_web_research_task(db_session)
    obj = {
        "title": "T",
        "summary": "S",
        "body": "B",
        "citations": ["https://example.com"],
        "authoring_branch": "feat/my-branch",
        "head_sha": "abc1234",
    }
    normalized = _normalize_document(obj, task)
    assert normalized["authoring_branch"] == "feat/my-branch"
    assert normalized["head_sha"] == "abc1234"


@pytest.mark.asyncio
async def test_normalize_document_provenance_absent_stores_none(db_session):
    """When authoring_branch/head_sha are absent, _normalize_document stores None."""
    from app.knowledge_completion import _normalize_document

    project, task = await _make_web_research_task(db_session)
    obj = {
        "title": "T",
        "summary": "S",
        "body": "B",
        "citations": ["https://example.com"],
    }
    normalized = _normalize_document(obj, task)
    assert normalized["authoring_branch"] is None
    assert normalized["head_sha"] is None


@pytest.mark.asyncio
async def test_persist_document_captures_provenance(db_session, _quiet_broadcast):
    """A document with authoring_branch/head_sha persists them on the KnowledgeDocument row."""
    project, task = await _make_web_research_task(db_session)
    document = {
        "title": "Provenance test doc",
        "summary": "Testing provenance capture.",
        "body": "# Body\n\nSome content.",
        "citations": ["https://example.com/provenance"],
        "tags": [],
        "topic": "provenance",
        "authoring_branch": "feat/provenance",
        "head_sha": "deadbeef",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    doc = docs[0]
    assert doc.authoring_branch == "feat/provenance"
    assert doc.head_sha == "deadbeef"


@pytest.mark.asyncio
async def test_persist_document_provenance_absent_stores_null(
    db_session, _quiet_broadcast
):
    """A document without authoring_branch/head_sha stores NULL on the row."""
    project, task = await _make_web_research_task(db_session)
    document = {
        "title": "No provenance doc",
        "summary": "No provenance fields.",
        "body": "# Body\n\nContent without provenance.",
        "citations": ["https://example.com/no-prov"],
        "tags": [],
        "topic": "no-provenance",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    doc = docs[0]
    assert doc.authoring_branch is None
    assert doc.head_sha is None


@pytest.mark.asyncio
async def test_knowledge_document_persists_finding_challenge_origin(
    db_session, _quiet_broadcast
):
    """A challenged research-spawned knowledge task persists durable origin."""
    research_task_id = str(uuid.uuid4())
    project, task = await _make_web_research_task(
        db_session,
        project_name="WRchallengeorigin",
        context={
            "research_task_id": research_task_id,
            "parent_research_task_id": research_task_id,
            "finding_index": 3,
            "finding_hash": "finding-hash-abc123",
            "direction_id": "d-origin123",
            "finding_challenge": {
                "challenged_boundary": "reproduce_absent",
                "first_verdict": "absent",
                "second_opinion_verdict": "present",
                "agreement": "disagrees",
                "reason": "The cited behavior moved but still reproduces.",
                "sidecar_task_id": "sidecar-1",
                "unbounded_extra": "not persisted",
            },
        },
    )
    document = {
        "title": "Origin challenge doc",
        "summary": "Challenge-aware origin capture.",
        "body": "# Body\n\nOrigin details.",
        "citations": ["https://example.com/origin"],
        "tags": ["origin"],
        "topic": "origin",
    }

    await _handle_web_research_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.origin_state == "research_finding"
    assert doc.origin_research_task_id == research_task_id
    assert doc.origin_parent_research_task_id == research_task_id
    assert doc.origin_finding_index == 3
    assert doc.origin_finding_hash == "finding-hash-abc123"
    assert doc.direction_id == "d-origin123"
    assert doc.origin_challenge_state == "overturned"
    assert doc.origin_challenge_record == {
        "challenged_boundary": "reproduce_absent",
        "first_verdict": "absent",
        "second_opinion_verdict": "present",
        "agreement": "disagrees",
        "reason": "The cited behavior moved but still reproduces.",
        "sidecar_task_id": "sidecar-1",
    }

    await db_session.refresh(task)
    mirror = task.plan_output["knowledge_document"]["origin_provenance"]
    assert mirror["origin_state"] == "research_finding"
    assert mirror["finding_hash"] == "finding-hash-abc123"
    assert mirror["challenge_state"] == "overturned"


@pytest.mark.asyncio
async def test_origin_provenance_legacy_null_serialization(
    client: AsyncClient, db_session
):
    """Legacy knowledge rows with NULL origin columns remain readable."""
    proj = await _seed_project_with_membership(db_session, "KDoriginlegacy")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    doc = await _seed_doc(
        db_session, proj.id, task.id, title="Legacy design", source_kind="design"
    )

    listed = await client.get(f"/api/admin/projects/{proj.id}/knowledge-documents")
    assert listed.status_code == 200
    origin = listed.json()["documents"][0]["origin_provenance"]
    assert origin["origin_state"] == "legacy_unknown"
    assert origin["challenge_state"] == "legacy_unknown"
    assert origin["finding_hash"] is None

    detail = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents/{doc.id}"
    )
    assert detail.status_code == 200
    assert detail.json()["origin_provenance"] == origin


@pytest.mark.asyncio
async def test_knowledge_index_durable_docs_outrank_newer_stubs(db_session):
    """Durable design/web_research docs outrank newer research_audit stubs within
    the 20-slot bound. Documents are ranked by source-kind tier (durable first),
    then creation date descending, so newer stubs do not crowd out older durable knowledge."""
    from app.knowledge_query import build_knowledge_index
    from datetime import timedelta

    proj = await _seed_project_with_membership(db_session, "KDrank")
    base_time = (await db_session.execute(select(func.now()))).scalar()

    # Create durable task sources.
    design_task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    web_task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(design_task)
    db_session.add(web_task)
    await db_session.commit()
    await db_session.refresh(design_task)
    await db_session.refresh(web_task)

    # Create older durable docs (should rank first).
    durable_design = KnowledgeDocument(
        project_id=proj.id,
        source_task_id=design_task.id,
        title="Durable design doc (old)",
        summary="Design knowledge.",
        body="# Design\n\nImportant design decisions.",
        topic="design",
        tags=["design"],
        source_citations=["backend/app/models.py"],
        source_kind="design",
        forward_quality_verdict={"passed": True, "strategy": "forward-quality"},
        created_at=base_time - timedelta(days=5),
    )
    durable_web = KnowledgeDocument(
        project_id=proj.id,
        source_task_id=web_task.id,
        title="Durable web research doc (old)",
        summary="Web research knowledge.",
        body="# Research\n\nWeb survey findings.",
        topic="web",
        tags=["web"],
        source_citations=["https://example.com"],
        source_kind="web_research",
        forward_quality_verdict={"passed": True, "strategy": "forward-quality"},
        created_at=base_time - timedelta(days=3),
    )
    db_session.add(durable_design)
    db_session.add(durable_web)
    await db_session.commit()

    # Create many newer research_audit stubs (should rank after durables).
    for i in range(25):
        stub_task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
        db_session.add(stub_task)
    await db_session.commit()

    stub_tasks = (
        (
            await db_session.execute(
                select(Task)
                .where(
                    Task.project_id == proj.id,
                    Task.id != design_task.id,
                    Task.id != web_task.id,
                )
                .order_by(Task.created_at)
            )
        )
        .scalars()
        .all()
    )

    for i, stub_task in enumerate(stub_tasks):
        stub = KnowledgeDocument(
            project_id=proj.id,
            source_task_id=stub_task.id,
            title=f"Research audit stub {i}",
            summary=f"{i} finding(s) recorded",
            body=f"Audit findings {i}.",
            topic="audit",
            tags=["audit"],
            source_citations=[],
            source_kind="research_audit",
            forward_quality_verdict={"passed": True, "strategy": "forward-quality"},
            created_at=base_time - timedelta(hours=1 - i),
        )
        db_session.add(stub)
    await db_session.commit()

    # Build the index (limited to 20 slots).
    index = await build_knowledge_index(db_session, proj.id, limit=20)

    # Both durable docs should be in the index.
    titles = {entry["title"] for entry in index}
    assert "Durable design doc (old)" in titles
    assert "Durable web research doc (old)" in titles

    # The durable docs should come before research_audit stubs in the ranking.
    durable_positions = [
        i
        for i, entry in enumerate(index)
        if entry["source_kind"] in ("design", "web_research")
    ]
    stub_positions = [
        i
        for i, entry in enumerate(index)
        if entry["source_kind"] == "research_audit"
    ]
    # All durable docs should appear before all stubs.
    if durable_positions and stub_positions:
        assert max(durable_positions) < min(stub_positions)


@pytest.mark.asyncio
async def test_origin_provenance_unknown_null_serialization(
    client: AsyncClient, db_session
):
    """Rows with source_kind='unknown' and NULL origin columns stay legacy."""
    proj = await _seed_project_with_membership(db_session, "KDoriginunknown")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    doc = await _seed_doc(
        db_session, proj.id, task.id, title="Legacy unknown", source_kind="unknown"
    )

    listed = await client.get(f"/api/admin/projects/{proj.id}/knowledge-documents")
    assert listed.status_code == 200
    origin = listed.json()["documents"][0]["origin_provenance"]
    assert origin["origin_state"] == "legacy_unknown"
    assert origin["challenge_state"] == "legacy_unknown"

    legacy = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={"origin_state": "legacy_unknown"},
    )
    assert legacy.status_code == 200
    assert legacy.json()["total"] == 1
    assert legacy.json()["documents"][0]["id"] == str(doc.id)

    challenge = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={"challenge_state": "legacy_unknown"},
    )
    assert challenge.status_code == 200
    assert challenge.json()["total"] == 1
    assert challenge.json()["documents"][0]["id"] == str(doc.id)

    non_derived = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={"origin_state": "not_research_finding_derived"},
    )
    assert non_derived.status_code == 200
    assert non_derived.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_filters_by_origin_challenge_and_finding_hash(
    client: AsyncClient, db_session
):
    """Origin/challenge/finding-hash filters are applied by the shared API."""
    proj = await _seed_project_with_membership(db_session, "KDoriginfilters")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _seed_doc(
        db_session,
        proj.id,
        task.id,
        title="Overturned origin",
        origin_state="research_finding",
        origin_challenge_state="overturned",
        origin_finding_hash="fh-overturned",
    )
    await _seed_doc(
        db_session,
        proj.id,
        task.id,
        title="Unchallenged origin",
        origin_state="research_finding",
        origin_challenge_state="unchallenged",
        origin_finding_hash="fh-unchallenged",
    )

    resp = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={
            "origin_state": "research_finding",
            "challenge_state": "overturned",
            "finding_hash": "fh-overturned",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["documents"][0]["title"] == "Overturned origin"
    assert body["documents"][0]["origin_provenance"]["finding_hash"] == (
        "fh-overturned"
    )


# ── build_knowledge_index: compact, bounded, scoped index ────────────────────


async def _make_knowledge_doc(
    db_session,
    *,
    project_id,
    source_task_id,
    title,
    summary="A summary.",
    topic="t",
    direction_id=None,
    source_kind="web_research",
    created_at=None,
    sweep_id=None,
    arm_label=None,
    forward_quality_verdict=None,
    source_citations=None,
):
    """Persist one KnowledgeDocument for index tests, with optional explicit
    created_at so ordering is deterministic within a single transaction."""
    doc = KnowledgeDocument(
        project_id=project_id,
        source_task_id=source_task_id,
        title=title,
        summary=summary,
        body="# Body\n\n" + ("x" * 50),
        topic=topic,
        direction_id=direction_id,
        source_kind=source_kind,
        sweep_id=sweep_id,
        arm_label=arm_label,
        forward_quality_verdict=forward_quality_verdict,
        source_citations=source_citations,
    )
    if created_at is not None:
        doc.created_at = created_at
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


@pytest.mark.asyncio
async def test_knowledge_index_entry_shape(db_session):
    """The index entry carries exactly the expected navigation fields (including sweep_id and arm_label) and never the body."""
    from app.knowledge_query import build_knowledge_index

    project, task = await _make_web_research_task(db_session, project_name="KI1")
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Doc A",
        summary="Summary A",
        topic="auth",
        direction_id="d-aaaa1111",
        source_kind="design",
    )

    index = await build_knowledge_index(db_session, project.id)
    assert len(index) == 1
    entry = index[0]
    assert set(entry.keys()) == {
        "id",
        "title",
        "summary",
        "topic",
        "direction_id",
        "source_kind",
        "analysis_fallback_reason",
        "created_at",
        "knowledge_acceptance_evidence",
        "compute_acceptance_grade",
        "compute_acceptance_reason",
        "unavailable_cause",
        "criterion_met",
        "criterion",
        "criterion_unmeasured_reason",
        "evidence_tier",
        "per_arm_verdicts",
        "origin_provenance",
        "run_provenance",
        "sweep_id",
        "arm_label",
        "lifecycle_status",
    }
    assert "body" not in entry
    assert entry["knowledge_acceptance_evidence"] is True
    assert entry["compute_acceptance_grade"] is None  # non-compute doc
    assert entry["compute_acceptance_reason"] == "non_compute_forward_quality_gate"
    assert entry["unavailable_cause"] is None
    assert entry["origin_provenance"]["origin_state"] == "legacy_unknown"
    assert entry["origin_provenance"]["challenge_state"] == "legacy_unknown"
    assert entry["title"] == "Doc A"
    assert entry["summary"] == "Summary A"
    assert entry["topic"] == "auth"
    assert entry["direction_id"] == "d-aaaa1111"
    assert entry["source_kind"] == "design"


@pytest.mark.asyncio
async def test_build_knowledge_index_empty_when_no_documents(db_session):
    """No documents → empty list (the caller omits the payload field entirely)."""
    from app.knowledge_query import build_knowledge_index

    project, _task = await _make_web_research_task(db_session, project_name="KI2")
    assert await build_knowledge_index(db_session, project.id) == []


@pytest.mark.asyncio
async def test_build_knowledge_index_respects_count_cap(db_session):
    """The entry count never exceeds the requested limit."""
    from app.knowledge_query import build_knowledge_index

    project, task = await _make_web_research_task(db_session, project_name="KI3")
    for i in range(5):
        await _make_knowledge_doc(
            db_session,
            project_id=project.id,
            source_task_id=task.id,
            title=f"Doc {i}",
        )
    index = await build_knowledge_index(db_session, project.id, limit=3)
    assert len(index) == 3


@pytest.mark.asyncio
async def test_build_knowledge_index_truncates_summary(db_session):
    """Long summaries are collapsed and truncated to summary_chars (with ellipsis)."""
    from app.knowledge_query import build_knowledge_index

    project, task = await _make_web_research_task(db_session, project_name="KI4")
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Long",
        summary="word " * 100,
    )
    index = await build_knowledge_index(db_session, project.id, summary_chars=40)
    assert len(index) == 1
    summary = index[0]["summary"]
    assert len(summary) <= 40
    assert summary.endswith("…")


@pytest.mark.asyncio
async def test_build_knowledge_index_newest_first(db_session):
    """Entries are ordered newest-first by created_at."""
    from datetime import datetime, timedelta

    from app.knowledge_query import build_knowledge_index

    project, task = await _make_web_research_task(db_session, project_name="KI5")
    base = datetime(2026, 1, 1, 12, 0, 0)
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Older",
        created_at=base,
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Newer",
        created_at=base + timedelta(hours=1),
    )
    index = await build_knowledge_index(db_session, project.id)
    assert [e["title"] for e in index] == ["Newer", "Older"]


@pytest.mark.asyncio
async def test_build_knowledge_index_direction_scope_first_then_project_fill(
    db_session,
):
    """Direction-scoped docs come first; project-wide docs fill the remainder,
    with no duplicates and total bounded by the limit."""
    from datetime import datetime, timedelta

    from app.knowledge_query import build_knowledge_index

    project, task = await _make_web_research_task(db_session, project_name="KI6")
    base = datetime(2026, 1, 1, 12, 0, 0)
    # Two project-wide (no direction) docs, NEWER than the direction-scoped one.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Proj newer 1",
        direction_id=None,
        created_at=base + timedelta(hours=5),
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Proj newer 2",
        direction_id=None,
        created_at=base + timedelta(hours=4),
    )
    # One direction-scoped doc, OLDER than the project-wide ones.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Dir doc",
        direction_id="d-zzzz9999",
        created_at=base,
    )

    index = await build_knowledge_index(
        db_session, project.id, direction_id="d-zzzz9999"
    )
    titles = [e["title"] for e in index]
    # Direction doc first despite being oldest; then project-wide fill newest-first.
    assert titles[0] == "Dir doc"
    assert set(titles) == {"Dir doc", "Proj newer 1", "Proj newer 2"}
    assert len(titles) == len(set(titles))  # no duplicates


@pytest.mark.asyncio
async def test_build_knowledge_index_excludes_cross_project(db_session):
    """Documents from other projects are never included."""
    from app.knowledge_query import build_knowledge_index

    project_a, task_a = await _make_web_research_task(db_session, project_name="KIA")
    project_b, task_b = await _make_web_research_task(db_session, project_name="KIB")
    await _make_knowledge_doc(
        db_session,
        project_id=project_a.id,
        source_task_id=task_a.id,
        title="A doc",
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project_b.id,
        source_task_id=task_b.id,
        title="B doc",
    )
    index_a = await build_knowledge_index(db_session, project_a.id)
    assert [e["title"] for e in index_a] == ["A doc"]


# ── build_compute_sweep_groups: per-sweep comparative arm grouping ───────────
def _acceptance_verdict():
    """forward_quality_verdict that classifies a compute doc as acceptance."""
    return {
        "analysis_mode": "llm",
        "criterion_met": True,
        "probes": {"grounding": {"grounded": 1}},
    }


async def _make_compute_sweep_task(
    db_session,
    *,
    project_id,
    sweep_id,
    arm_label,
    status=TaskStatus.COMPLETED,
    direction_id=None,
    created_at=None,
):
    payload = {
        "type": "compute_experiment",
        "base_branch": "main",
        "sweep_id": sweep_id,
        "experiment": arm_label,
    }
    task = Task(
        project_id=project_id,
        title=f"compute arm {arm_label}",
        status=status,
        payloads=[payload],
        context=({"direction_id": direction_id} if direction_id else {}),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    if created_at is not None:
        task.created_at = created_at
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _make_compute_sweep_doc(
    db_session,
    *,
    project_id,
    task,
    title=None,
    sweep_id=None,
    arm_label=None,
    criterion_met=True,
    unavailable_cause=None,
    forward_quality_verdict=None,
    source_citations=None,
    created_at=None,
):
    verdict = (
        dict(forward_quality_verdict)
        if isinstance(forward_quality_verdict, dict)
        else {"analysis_mode": "deterministic", "criterion_met": criterion_met}
    )
    if unavailable_cause is not None:
        verdict["unavailable_cause"] = unavailable_cause
    payload = task.first_payload or {}
    return await _make_knowledge_doc(
        db_session,
        project_id=project_id,
        source_task_id=task.id,
        title=title or f"arm {arm_label or payload.get('experiment')}",
        source_kind="compute_experiment",
        sweep_id=sweep_id if sweep_id is not None else payload.get("sweep_id"),
        arm_label=arm_label if arm_label is not None else payload.get("experiment"),
        forward_quality_verdict=verdict,
        source_citations=source_citations,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_requires_all_planned_unique_arms(
    db_session,
):
    """Only complete planned rosters become strategist-facing sweep groups."""
    from app.knowledge_query import build_compute_sweep_groups

    project, _task = await _make_web_research_task(db_session, project_name="SWG1")
    baseline = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-1111",
        arm_label="baseline",
    )
    treatment = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-1111",
        arm_label="treatment",
    )
    missing = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-missing",
        arm_label="baseline",
    )
    await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-missing",
        arm_label="treatment",
    )
    running = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-running",
        arm_label="baseline",
    )
    still_running = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-running",
        arm_label="treatment",
        status=TaskStatus.IN_PROGRESS,
    )

    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=baseline, criterion_met=True
    )
    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=treatment, criterion_met=False
    )
    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=missing, criterion_met=True
    )
    await _make_compute_sweep_doc(db_session, project_id=project.id, task=running)
    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=still_running
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert set(groups) == {"sw-1111"}
    assert {e["arm_label"] for e in groups["sw-1111"]} == {
        "baseline",
        "treatment",
    }
    assert {e["criterion_met"] for e in groups["sw-1111"]} == {True, False}


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_excludes_null_sweep_and_single_arm(
    db_session,
):
    """Docs with no sweep_id are excluded; single-arm sweeps are not comparative."""
    from app.knowledge_query import build_compute_sweep_groups

    project, task = await _make_web_research_task(db_session, project_name="SWG2")
    # Null-sweep compute doc — excluded.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="no-sweep",
        source_kind="compute_experiment",
        sweep_id=None,
        arm_label="lonely",
    )
    # Single-arm sweep — excluded (not comparative).
    solo = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-solo",
        arm_label="only-arm",
    )
    await _make_compute_sweep_doc(db_session, project_id=project.id, task=solo)
    groups = await build_compute_sweep_groups(db_session, project.id)
    assert groups == {}


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_newest_first_and_bounded(db_session):
    """Sweep groups are newest-first by latest arm and capped at the limit."""
    from datetime import datetime, timedelta

    from app.knowledge_query import build_compute_sweep_groups

    project, _task = await _make_web_research_task(db_session, project_name="SWG3")
    base = datetime(2026, 1, 1, 12, 0, 0)
    # Three sweeps, each with two arms, at increasing timestamps.
    for i in range(3):
        for arm in ("a", "b"):
            arm_task = await _make_compute_sweep_task(
                db_session,
                project_id=project.id,
                sweep_id=f"sw-{i}",
                arm_label=arm,
                created_at=base + timedelta(hours=i),
            )
            await _make_compute_sweep_doc(
                db_session,
                project_id=project.id,
                task=arm_task,
                title=f"sweep{i}-{arm}",
                created_at=base + timedelta(hours=i),
            )
    groups = await build_compute_sweep_groups(db_session, project.id, limit=2)
    # Returns {sweep_id: [entries]}; capped at 2, newest sweep first (sw-2 has the latest arm).
    assert len(groups) == 2
    sweep_ids = list(groups.keys())
    assert sweep_ids[0] == "sw-2"
    assert sweep_ids[1] == "sw-1"


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_excludes_cross_project(db_session):
    """Sweep docs from other projects are never included."""
    from app.knowledge_query import build_compute_sweep_groups

    project_a, _task_a = await _make_web_research_task(
        db_session, project_name="SWGA"
    )
    project_b, _task_b = await _make_web_research_task(
        db_session, project_name="SWGB"
    )
    for arm in ("a", "b"):
        arm_task = await _make_compute_sweep_task(
            db_session,
            project_id=project_b.id,
            sweep_id="sw-b",
            arm_label=arm,
        )
        await _make_compute_sweep_doc(
            db_session, project_id=project_b.id, task=arm_task, title=f"b-{arm}"
        )
    assert await build_compute_sweep_groups(db_session, project_a.id) == {}


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_dedupes_retried_arm_documents(
    db_session,
):
    from datetime import datetime, timedelta

    from app.knowledge_query import build_compute_sweep_groups

    project, _task = await _make_web_research_task(db_session, project_name="SWGRetry")
    base = datetime(2026, 1, 1, 12, 0, 0)
    baseline_old = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-retry",
        arm_label="baseline",
        created_at=base,
    )
    baseline_new = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-retry",
        arm_label="baseline",
        created_at=base + timedelta(minutes=1),
    )
    treatment = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-retry",
        arm_label="treatment",
        created_at=base + timedelta(minutes=2),
    )
    await _make_compute_sweep_doc(
        db_session,
        project_id=project.id,
        task=baseline_old,
        title="old baseline",
        created_at=base,
    )
    await _make_compute_sweep_doc(
        db_session,
        project_id=project.id,
        task=baseline_new,
        title="new baseline",
        created_at=base + timedelta(minutes=1),
    )
    await _make_compute_sweep_doc(
        db_session,
        project_id=project.id,
        task=treatment,
        title="treatment",
        created_at=base + timedelta(minutes=2),
    )

    entries = (await build_compute_sweep_groups(db_session, project.id))["sw-retry"]
    assert {entry["arm_label"] for entry in entries} == {"baseline", "treatment"}
    baseline_entry = next(
        entry for entry in entries if entry["arm_label"] == "baseline"
    )
    assert baseline_entry["title"] == "new baseline"


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_omits_unusable_planned_arms(
    db_session,
):
    from app.knowledge_query import build_compute_sweep_groups, build_sweep_knowledge_index

    project, _task = await _make_web_research_task(db_session, project_name="SWGBad")
    unavailable = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-unavailable",
        arm_label="baseline",
    )
    ok = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-unavailable",
        arm_label="treatment",
    )
    await _make_compute_sweep_doc(
        db_session,
        project_id=project.id,
        task=unavailable,
        criterion_met=False,
        unavailable_cause="compute_failed",
    )
    await _make_compute_sweep_doc(db_session, project_id=project.id, task=ok)

    assert await build_compute_sweep_groups(db_session, project.id) == {}
    raw = await build_sweep_knowledge_index(db_session, project.id, "sw-unavailable")
    assert {entry["arm_label"] for entry in raw} == {"baseline", "treatment"}


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_respects_direction_scope(db_session):
    from app.knowledge_query import build_compute_sweep_groups

    project, _task = await _make_web_research_task(db_session, project_name="SWGDir")
    for arm in ("baseline", "treatment"):
        arm_task = await _make_compute_sweep_task(
            db_session,
            project_id=project.id,
            sweep_id="sw-dir-a",
            arm_label=arm,
            direction_id="d-a",
        )
        await _make_compute_sweep_doc(
            db_session, project_id=project.id, task=arm_task
        )
    arm_task = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-dir-b",
        arm_label="baseline",
        direction_id="d-b",
    )
    await _make_compute_sweep_doc(db_session, project_id=project.id, task=arm_task)

    scoped = await build_compute_sweep_groups(
        db_session, project.id, direction_id="d-a"
    )
    assert set(scoped) == {"sw-dir-a"}
    assert await build_compute_sweep_groups(
        db_session, project.id, direction_id="d-b"
    ) == {}


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_surfaces_per_arm_value_and_criterion(
    db_session,
):
    """Sweep groups include per-arm metric values and criterion so the strategist
    can tell 'which arm won by how much' without fetching each document body."""
    from app.knowledge_query import build_compute_sweep_groups

    project, _task = await _make_web_research_task(
        db_session, project_name="SWGMetric"
    )

    # Create two arms with a criterion and per-arm verdicts
    verdict_arm_a = {
        "analysis_mode": "deterministic",
        "strategy": "metric",
        "outcome": "SUCCESS",
        "cost_usd": 0.05,
        "criterion": {
            "metric": "accuracy",
            "operator": ">=",
            "threshold": 0.85,
            "value": 0.92,
            "measurable": True,
            "met": True,
        },
        "criterion_met": True,
        "per_arm_verdicts": {
            "baseline": {"value": 0.88, "met": False, "cost_usd": 0.025},
            "treatment": {"value": 0.92, "met": True, "cost_usd": 0.025},
        },
        "probes": {"grounding": {"grounded": 1}},
    }

    verdict_arm_b = {
        "analysis_mode": "deterministic",
        "strategy": "metric",
        "outcome": "SUCCESS",
        "cost_usd": 0.05,
        "criterion": {
            "metric": "accuracy",
            "operator": ">=",
            "threshold": 0.85,
            "value": 0.92,
            "measurable": True,
            "met": True,
        },
        "criterion_met": True,
        "per_arm_verdicts": {
            "baseline": {"value": 0.88, "met": False, "cost_usd": 0.025},
            "treatment": {"value": 0.92, "met": True, "cost_usd": 0.025},
        },
        "probes": {"grounding": {"grounded": 1}},
    }

    baseline = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-metric",
        arm_label="baseline",
    )
    await _make_compute_sweep_doc(
        db_session,
        project_id=project.id,
        task=baseline,
        title="arm baseline",
        forward_quality_verdict=verdict_arm_a,
        source_citations=["gs://bucket/result.json"],
    )
    treatment = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-metric",
        arm_label="treatment",
    )
    await _make_compute_sweep_doc(
        db_session,
        project_id=project.id,
        task=treatment,
        title="arm treatment",
        forward_quality_verdict=verdict_arm_b,
        source_citations=["gs://bucket/result.json"],
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-metric" in groups
    entries = groups["sw-metric"]
    assert len(entries) == 2

    by_label = {e["arm_label"]: e for e in entries}

    # Both arms should have the per-arm values available in the payload
    for arm_name in ["baseline", "treatment"]:
        entry = by_label[arm_name]
        # Entry should have criterion information
        assert entry["criterion"] is not None
        assert entry["criterion"]["metric"] == "accuracy"
        assert entry["criterion"]["threshold"] == 0.85
        # Entry should have per_arm_verdicts with measured values
        assert entry["per_arm_verdicts"] is not None
        assert "baseline" in entry["per_arm_verdicts"]
        assert "treatment" in entry["per_arm_verdicts"]
        # Each arm should have value and met status
        assert entry["per_arm_verdicts"]["baseline"]["value"] == 0.88
        assert entry["per_arm_verdicts"]["baseline"]["met"] is False
        assert entry["per_arm_verdicts"]["treatment"]["value"] == 0.92
        assert entry["per_arm_verdicts"]["treatment"]["met"] is True


# ── build_compute_sweep_groups: planned-arm completeness gate ─────────────────


async def _make_compute_sweep_task(
    db_session,
    *,
    project_id,
    sweep_id,
    arm_label=None,
    status=None,
    direction_id=None,
    created_at=None,
):
    """Persist a compute_experiment Task carrying sweep_id in its first payload."""
    from app.models import TaskStatus

    task = make_task(
        project_id=project_id,
        title=f"compute_experiment: arm {arm_label or sweep_id}",
        status=status or TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "experiment": arm_label or "sweep-test",
                "base_branch": "main",
                "sweep_id": sweep_id,
            }
        ],
    )
    if created_at is not None:
        task.created_at = created_at
    if direction_id:
        task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_blocks_incomplete_planned_arms(db_session):
    """A sweep with 3 planned arms but only 2 usable docs is excluded.

    The acceptance requirement for this task: the test must exercise the
    completeness gate that prevents partial-sweep overclaim.
    """
    from app.knowledge_query import build_compute_sweep_groups

    project = make_project(name="SWGIncomplete")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Three planned arms — each backed by a Task row.
    task_a = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-inc", arm_label="a"
    )
    task_b = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-inc", arm_label="b"
    )
    task_c = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-inc", arm_label="c"
    )

    # Only two arms produced usable docs (task_c is missing).
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_a.id,
        title="arm a",
        source_kind="compute_experiment",
        sweep_id="sw-inc",
        arm_label="a",
        forward_quality_verdict=_acceptance_verdict(),
        source_citations=["gs://bucket/a.json"],
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_b.id,
        title="arm b",
        source_kind="compute_experiment",
        sweep_id="sw-inc",
        arm_label="b",
        forward_quality_verdict=_acceptance_verdict(),
        source_citations=["gs://bucket/b.json"],
    )
    # task_c has no KnowledgeDocument — arm c never produced results.
    _ = task_c  # referenced to make intent explicit

    # The sweep has 2 docs (>= _SWEEP_MIN_DOCS) but the task manifest shows 3
    # planned arms; the missing arm blocks the sweep.
    groups = await build_compute_sweep_groups(db_session, project.id)
    assert groups == {}, (
        "Sweep with an unrepresented planned arm must be excluded from sweep groups"
    )


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_blocks_when_arm_doc_unavailable(db_session):
    """A sweep where one arm's doc carries unavailable_cause is excluded."""
    from app.knowledge_query import build_compute_sweep_groups

    project = make_project(name="SWGUnavail")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task_a = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-unav", arm_label="a"
    )
    task_b = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-unav", arm_label="b"
    )

    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_a.id,
        title="arm a ok",
        source_kind="compute_experiment",
        sweep_id="sw-unav",
        arm_label="a",
        forward_quality_verdict=_acceptance_verdict(),
        source_citations=["gs://bucket/a.json"],
    )
    # arm b's doc has unavailable_cause — not usable.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_b.id,
        title="arm b unavailable",
        source_kind="compute_experiment",
        sweep_id="sw-unav",
        arm_label="b",
        forward_quality_verdict={"unavailable_cause": "analysis_timed_out"},
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert groups == {}, (
        "Sweep with an unavailable arm doc must be excluded from sweep groups"
    )


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_respects_direction_scope_for_manifest(
    db_session,
):
    """A same-sweep_id manifest in another direction must not block the scoped view."""
    from app.knowledge_query import build_compute_sweep_groups

    project = make_project(name="SWGDirectionScoped")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task_a = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-shared",
        arm_label="a1",
        direction_id="d-a",
    )
    task_b = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-shared",
        arm_label="a2",
        direction_id="d-a",
    )
    task_other = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-shared",
        arm_label="b1",
        direction_id="d-b",
    )

    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_a.id,
        title="arm a1",
        direction_id="d-a",
        source_kind="compute_experiment",
        sweep_id="sw-shared",
        arm_label="a1",
        forward_quality_verdict=_acceptance_verdict(),
        source_citations=["gs://bucket/a1.json"],
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_b.id,
        title="arm a2",
        direction_id="d-a",
        source_kind="compute_experiment",
        sweep_id="sw-shared",
        arm_label="a2",
        forward_quality_verdict=_acceptance_verdict(),
        source_citations=["gs://bucket/a2.json"],
    )
    # Same sweep_id in another direction, but no usable doc.
    _ = task_other

    groups = await build_compute_sweep_groups(
        db_session,
        project.id,
        direction_id="d-a",
    )
    assert "sw-shared" in groups
    assert {entry["direction_id"] for entry in groups["sw-shared"]} == {"d-a"}


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_allows_measured_only_doc(db_session):
    """A measured-only doc (no unavailable_cause) does not block the sweep."""
    from app.knowledge_query import build_compute_sweep_groups

    project = make_project(name="SWGMeasured")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task_a = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-meas", arm_label="a"
    )
    task_b = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-meas", arm_label="b"
    )

    # arm a: acceptance verdict.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_a.id,
        title="arm a",
        source_kind="compute_experiment",
        sweep_id="sw-meas",
        arm_label="a",
        forward_quality_verdict=_acceptance_verdict(),
        source_citations=["gs://bucket/a.json"],
    )
    # arm b: deterministic/measured-only verdict — no unavailable_cause.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_b.id,
        title="arm b measured",
        source_kind="compute_experiment",
        sweep_id="sw-meas",
        arm_label="b",
        forward_quality_verdict={"analysis_mode": "deterministic", "outcome": "SUCCESS"},
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-meas" in groups, (
        "Sweep where all arms have usable (measured-only) docs must be included"
    )
    assert len(groups["sw-meas"]) == 2


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_falls_back_to_docs_only_when_no_task_manifest(
    db_session,
):
    """When no Task rows carry the sweep_id, docs-only (>= 2) behavior is kept."""
    from app.knowledge_query import build_compute_sweep_groups

    project, web_task = await _make_web_research_task(
        db_session, project_name="SWGFallback"
    )
    # Two compute docs with a sweep_id but sourced from a web_research task
    # (type != 'compute_experiment') — no task manifest will be found.
    for label in ("x", "y"):
        await _make_knowledge_doc(
            db_session,
            project_id=project.id,
            source_task_id=web_task.id,
            title=f"arm {label}",
            source_kind="compute_experiment",
            sweep_id="sw-fallback",
            arm_label=label,
        )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-fallback" in groups, (
        "Sweep with no task manifest must fall back to docs-only inclusion"
    )


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_includes_single_doc_arms_block(db_session):
    """A lone compute doc with >=2 measured per-arm verdicts is still a completed sweep."""
    from app.knowledge_query import build_compute_sweep_groups

    project, task = await _make_web_research_task(db_session, project_name="SWGBlock")
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="arms block",
        source_kind="compute_experiment",
        sweep_id="sw-block",
        arm_label="arms-block",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": True,
            "criterion": {"metric": "accuracy", "value": 0.92},
            "per_arm_verdicts": {
                "baseline": {"value": 0.88, "met": False},
                "treatment": {"value": 0.92, "met": True},
            },
        },
        source_citations=["gs://bucket/result.json"],
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="one-arm block",
        source_kind="compute_experiment",
        sweep_id="sw-solo",
        arm_label="arms-block",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": True,
            "criterion": {"metric": "accuracy", "value": 0.91},
            "per_arm_verdicts": {
                "only": {"value": 0.91, "met": True},
            },
        },
        source_citations=["gs://bucket/result.json"],
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-block" in groups
    assert "sw-solo" not in groups
    assert len(groups) == 1

    entries = groups["sw-block"]
    assert len(entries) == 2
    by_label = {e["arm_label"]: e for e in entries}
    assert by_label["baseline"]["criterion_met"] is False
    assert by_label["treatment"]["criterion_met"] is True


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_single_doc_blocks_unmeasured_arm(db_session):
    """A lone-doc sweep whose per_arm_verdicts has an unmeasured arm (met=None) is excluded.

    This exercises the guard that prevents partial-sweep overclaim when the arms block
    is incomplete — the invariant that every arm must carry a boolean met value.
    """
    from app.knowledge_query import build_compute_sweep_groups

    project, task = await _make_web_research_task(db_session, project_name="SWGUnmeasured")
    # per_arm_verdicts has one boolean arm and one unmeasured arm (met=None).
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="partial arms block",
        source_kind="compute_experiment",
        sweep_id="sw-partial",
        arm_label="arms-block",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": True,
            "criterion": {"metric": "accuracy", "value": 0.92},
            "per_arm_verdicts": {
                "baseline": {"value": 0.88, "met": False},
                "treatment": {"value": None, "met": None},  # unmeasured — must disqualify
            },
        },
        source_citations=["gs://bucket/result.json"],
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-partial" not in groups, (
        "A per_arm_verdicts block with an unmeasured arm must not be synthesised"
    )


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_single_doc_blocks_when_sibling_task_missing_doc(
    db_session,
):
    """A single-doc sweep is excluded when a sibling compute_experiment task lacks a doc.

    Even though the one document's per_arm_verdicts appears fully measured, the Task
    manifest shows a second planned arm with no usable KnowledgeDocument — the sweep
    must be excluded to enforce the completeness invariant.
    """
    from app.knowledge_query import build_compute_sweep_groups

    project = make_project(name="SWGSiblingMissing")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # task_main produces the single document.
    task_main = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-sibling", arm_label="main"
    )
    # task_sibling is a second planned arm — it never produced a KnowledgeDocument.
    await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-sibling", arm_label="sibling"
    )

    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task_main.id,
        title="main arm result",
        source_kind="compute_experiment",
        sweep_id="sw-sibling",
        arm_label="arms-block",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": True,
            "criterion": {"metric": "accuracy", "value": 0.92},
            "per_arm_verdicts": {
                "baseline": {"value": 0.88, "met": False},
                "treatment": {"value": 0.92, "met": True},
            },
        },
        source_citations=["gs://bucket/result.json"],
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-sibling" not in groups, (
        "Single-doc sweep with an unrepresented sibling task must be excluded"
    )


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_single_doc_with_manifest_included(db_session):
    """A lone-doc sweep backed by a compute_experiment Task manifest is included.

    This exercises the manifest-gating positive path: a single compute_experiment
    Task with the same sweep_id exists, the document is sourced from that task
    (so it is usable), and the per_arm_verdicts are fully measured — the sweep
    must be promoted to a completed group entry.
    """
    from app.knowledge_query import build_compute_sweep_groups

    project = make_project(name="SWGManifestOK")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One compute_experiment task — the manifest contains exactly this task.
    task = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id="sw-manifest-ok", arm_label="arms"
    )

    # One document sourced from that task with fully measured per_arm_verdicts.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="single-doc arms block",
        source_kind="compute_experiment",
        sweep_id="sw-manifest-ok",
        arm_label="arms-block",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": True,
            "criterion": {"metric": "accuracy", "value": 0.92},
            "per_arm_verdicts": {
                "baseline": {"value": 0.88, "met": False},
                "treatment": {"value": 0.92, "met": True},
            },
        },
        source_citations=["gs://bucket/result.json"],
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-manifest-ok" in groups, (
        "Single-doc sweep whose sole compute_experiment task produced a usable doc "
        "must be included via the manifest-gating path"
    )
    entries = groups["sw-manifest-ok"]
    assert len(entries) == 2
    by_label = {e["arm_label"]: e for e in entries}
    assert by_label["baseline"]["criterion_met"] is False
    assert by_label["treatment"]["criterion_met"] is True


@pytest.mark.asyncio
async def test_analyze_unavailable_stamps_cause_on_existing_doc(
    db_session, _quiet_broadcast
):
    """analyze_compute_experiment_knowledge stamps unavailable_cause +
    analysis_attempted_at on the stored doc when analysis is unavailable,
    without touching analysis_mode."""
    from unittest.mock import patch

    from app.knowledge_completion import (
        analyze_compute_experiment_knowledge,
        persist_compute_experiment_knowledge,
    )

    _project, task = await _make_compute_task(db_session, project_name="AnalyzeUnavailable")

    # First, persist the deterministic document (as completion would have done).
    doc = await persist_compute_experiment_knowledge(
        task,
        outcome="SUCCESS",
        cost=0.50,
        criterion=None,
        result_reference=None,
        session=db_session,
        logs=None,
    )
    assert doc is not None
    assert doc.forward_quality_verdict.get("analysis_mode") == "deterministic"

    # Trigger analysis with LLM disabled — analysis must end unavailable.
    with patch.dict("os.environ", {"DISABLE_LLM_COMPUTE_ANALYSIS": "1"}):
        result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.50,
            criterion=None,
            result_reference=None,
            session=db_session,
            logs=None,
        )

    assert result["status"] == "analysis_unavailable"
    assert result["reason"] == "analysis-disabled"
    assert result.get("knowledge_document_id") == str(doc.id)

    # The stored doc must carry the unavailable_cause stamp.
    await db_session.refresh(doc)
    verdict = doc.forward_quality_verdict
    assert verdict.get("unavailable_cause") == "analysis-disabled"
    assert verdict.get("analysis_attempted_at") is not None
    # analysis_mode must NOT have changed (still deterministic).
    assert verdict.get("analysis_mode") == "deterministic"


@pytest.mark.asyncio
async def test_analyze_transient_llm_error_stamps_recoverable_cause(
    db_session, _quiet_broadcast
):
    """A transient LLM/runtime failure surfaces the recoverable ``llm-error``
    cause (distinct from the terminal ``analysis-disabled``/``no-evidence`` causes
    the auto-delivery path classifies as never-retry). The deterministic doc is
    left intact and measured-only so acceptance credit stays withheld.
    """
    from unittest.mock import AsyncMock, patch

    from app.knowledge_completion import (
        analyze_compute_experiment_knowledge,
        persist_compute_experiment_knowledge,
    )

    _project, task = await _make_compute_task(
        db_session, project_name="AnalyzeTransient"
    )

    gs_uri = "gs://test-bucket/jobs/transient/result.json"

    # Persist the deterministic document first (as completion would have).
    doc = await persist_compute_experiment_knowledge(
        task,
        outcome="SUCCESS",
        cost=0.50,
        criterion=None,
        result_reference=gs_uri,
        session=db_session,
        logs=None,
    )
    assert doc is not None
    assert doc.forward_quality_verdict.get("analysis_mode") == "deterministic"

    # Force durable evidence to be present (so the path reaches the LLM call) and
    # make the LLM call itself fail transiently → reason "llm-error".
    fake_evidence = {
        "job_id": "transient-job",
        "artifact_uri": gs_uri,
        "file_count": 2,
        "metrics": {"accuracy": 0.93},
        "citations": [gs_uri, f"{gs_uri}#metric:accuracy=0.93"],
        "real_evidence": [gs_uri, f"{gs_uri}#metric:accuracy=0.93"],
        "log_excerpt": "",
    }
    with (
        patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch(
            "app.knowledge_completion._gather_compute_evidence",
            new=AsyncMock(return_value=fake_evidence),
        ),
        patch(
            "app.knowledge_completion._call_compute_analysis_llm",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.50,
            criterion=None,
            result_reference=gs_uri,
            session=db_session,
            logs=None,
        )

    assert result["status"] == "analysis_unavailable"
    assert result["reason"] == "llm-error"

    await db_session.refresh(doc)
    verdict = doc.forward_quality_verdict
    assert verdict.get("unavailable_cause") == "llm-error"
    # Still measured-only: a recoverable failure must not earn acceptance credit.
    assert verdict.get("analysis_mode") == "deterministic"
    assert verdict.get("knowledge_acceptance_evidence") is False


# ── fetch_direction_knowledge_index ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_direction_knowledge_index_groups_by_direction_id(db_session):
    """fetch_direction_knowledge_index returns per-direction lists, newest-first,
    and excludes directions with no documents."""
    from app.knowledge_query import fetch_direction_knowledge_index

    project, task = await _make_web_research_task(db_session, project_name="FDK1")
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Dir A doc 1",
        direction_id="d-aaa",
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Dir A doc 2",
        direction_id="d-aaa",
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="Dir B doc 1",
        direction_id="d-bbb",
    )

    result = await fetch_direction_knowledge_index(
        db_session, project.id, ["d-aaa", "d-bbb", "d-ccc"]
    )
    assert set(result.keys()) == {"d-aaa", "d-bbb"}
    assert len(result["d-aaa"]) == 2
    assert len(result["d-bbb"]) == 1
    assert "d-ccc" not in result


@pytest.mark.asyncio
async def test_fetch_direction_knowledge_index_caps_per_direction(db_session):
    """No more than _DIRECTION_KNOWLEDGE_CAP entries per direction."""
    from app.knowledge_query import fetch_direction_knowledge_index, _DIRECTION_KNOWLEDGE_CAP

    project, task = await _make_web_research_task(db_session, project_name="FDK2")
    for i in range(_DIRECTION_KNOWLEDGE_CAP + 3):
        await _make_knowledge_doc(
            db_session,
            project_id=project.id,
            source_task_id=task.id,
            title=f"Doc {i}",
            direction_id="d-overflow",
        )

    result = await fetch_direction_knowledge_index(
        db_session, project.id, ["d-overflow"]
    )
    assert len(result["d-overflow"]) == _DIRECTION_KNOWLEDGE_CAP


@pytest.mark.asyncio
async def test_fetch_direction_knowledge_index_empty_direction_list(db_session):
    """Empty direction_ids returns empty dict without querying DB."""
    from app.knowledge_query import fetch_direction_knowledge_index

    project, _ = await _make_web_research_task(db_session, project_name="FDK3")
    result = await fetch_direction_knowledge_index(db_session, project.id, [])
    assert result == {}


@pytest.mark.asyncio
async def test_sweep_id_filter_scopes_to_sweep(client: AsyncClient, db_session):
    """?sweep_id=<id> returns only documents for that sweep, excluding others."""
    proj = await _seed_project_with_membership(db_session, "SweepFilter")
    task = make_task(project_id=proj.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _seed_doc(
        db_session, proj.id, task.id,
        title="Arm alpha result",
        source_kind="compute_experiment",
        sweep_id="swp-aaa",
        arm_label="alpha",
    )
    await _seed_doc(
        db_session, proj.id, task.id,
        title="Arm beta result",
        source_kind="compute_experiment",
        sweep_id="swp-aaa",
        arm_label="beta",
    )
    await _seed_doc(
        db_session, proj.id, task.id,
        title="Different sweep doc",
        source_kind="compute_experiment",
        sweep_id="swp-bbb",
        arm_label="gamma",
    )

    resp = await client.get(
        f"/api/admin/projects/{proj.id}/knowledge-documents",
        params={"sweep_id": "swp-aaa"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    titles = {d["title"] for d in body["documents"]}
    assert titles == {"Arm alpha result", "Arm beta result"}
    # sweep_id and arm_label must appear in the serialized documents
    for doc in body["documents"]:
        assert doc["sweep_id"] == "swp-aaa"
        assert doc["arm_label"] in {"alpha", "beta"}


# ── _build_compute_experiment_document failed-arms section ───────────────────


def test_build_compute_experiment_document_failed_arms_dict():
    """Dict-shaped arms: ### Failed arms section appears for arms with exit_code != 0 or error."""
    from app.knowledge_completion import _build_compute_experiment_document

    task = make_task(
        title="compute_experiment: exp-fail",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "experiment": "exp-fail"}],
    )

    arms = {
        "adam": {
            "val_loss": 0.42,
            "exit_code": 1,
            "error": "RuntimeError: CUDA OOM",
            "stderr_excerpt": "Traceback (most recent call last): ...",
        },
        "sgd": {"val_loss": 0.61},
    }

    result = _build_compute_experiment_document(
        task,
        outcome="FAILED",
        cost=0.10,
        criterion=None,
        result_reference=None,
        arms=arms,
    )

    body = result["body"]
    assert "### Failed arms" in body
    assert "adam" in body
    failed_section = body.split("### Failed arms")[-1]
    assert "FAILED" in failed_section
    assert "RuntimeError: CUDA OOM" in failed_section
    assert "Traceback" in failed_section
    # sgd succeeded — must not appear in the failed section
    assert "sgd" not in failed_section


def test_build_compute_experiment_document_failed_arms_list():
    """List-shaped arms: ### Failed arms section appears for arms using 'reason' field."""
    from app.knowledge_completion import _build_compute_experiment_document

    task = make_task(
        title="compute_experiment: exp-list",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "experiment": "exp-list"}],
    )

    arms = [
        {"name": "arm_a", "reason": "result file not written"},
        {"name": "arm_b", "val_loss": 0.55},
    ]

    result = _build_compute_experiment_document(
        task,
        outcome="FAILED",
        cost=0.05,
        criterion=None,
        result_reference=None,
        arms=arms,
    )

    body = result["body"]
    assert "### Failed arms" in body
    failed_section = body.split("### Failed arms")[-1]
    assert "arm_a" in failed_section
    assert "result file not written" in failed_section
    # arm_b succeeded — must not appear in the failed section
    assert "arm_b" not in failed_section


# ── criterion_met surfacing in knowledge index and sweep groups ───────────────


@pytest.mark.asyncio
async def test_compute_sweep_verdict_reflects_criterion_met(db_session):
    """_knowledge_index_entry exposes criterion_met from forward_quality_verdict,
    independent of compute_acceptance_grade.

    A compute_experiment doc with criterion_met=True and grade 'measured_only'
    (not 'acceptance') must surface criterion_met=True in the index entry —
    proving the verdict is NOT derived from the acceptance grade.
    A doc with criterion_met=False must surface False; a doc with no
    criterion_met key in its verdict must surface None.
    """
    from app.knowledge_query import build_compute_sweep_groups, build_knowledge_index

    project, _task = await _make_web_research_task(db_session, project_name="CRIT")

    met_task = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-crit",
        arm_label="met_arm",
    )
    not_met_task = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-crit",
        arm_label="not_met_arm",
    )
    unmeasured_task = await _make_compute_sweep_task(
        db_session,
        project_id=project.id,
        sweep_id="sw-crit",
        arm_label="unmeasured_arm",
    )

    # ARM 1: criterion MET but grade is measured_only (deterministic path).
    # This is the bug-case: old code would have shown NOT MET because grade != acceptance.
    met_doc = await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=met_task.id,
        title="arm met",
        source_kind="compute_experiment",
        sweep_id="sw-crit",
        arm_label="met_arm",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": True,
            "criterion": {"metric": "accuracy", "value": 0.95},
        },
        # No source_citations → measured_only grade (not acceptance).
        source_citations=None,
    )

    # ARM 2: criterion NOT MET, also measured_only grade.
    not_met_doc = await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=not_met_task.id,
        title="arm not met",
        source_kind="compute_experiment",
        sweep_id="sw-crit",
        arm_label="not_met_arm",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": False,
            "criterion": {"metric": "accuracy", "value": 0.72},
        },
        source_citations=None,
    )

    # ARM 3: unmeasured — forward_quality_verdict has no criterion_met key.
    unmeasured_doc = await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=unmeasured_task.id,
        title="arm unmeasured",
        source_kind="compute_experiment",
        sweep_id="sw-crit",
        arm_label="unmeasured_arm",
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            # criterion_met absent → None
        },
        source_citations=None,
    )

    # --- via build_knowledge_index ---
    index = await build_knowledge_index(db_session, project.id)
    by_id = {e["id"]: e for e in index}

    met_entry = by_id[str(met_doc.id)]
    not_met_entry = by_id[str(not_met_doc.id)]
    unmeasured_entry = by_id[str(unmeasured_doc.id)]

    # Verify grade is NOT 'acceptance' for met_arm — confirms verdict is not grade-derived.
    assert met_entry["compute_acceptance_grade"] != "acceptance"
    assert met_entry["criterion_met"] is True

    assert not_met_entry["compute_acceptance_grade"] != "acceptance"
    assert not_met_entry["criterion_met"] is False

    assert unmeasured_entry["criterion_met"] is None

    # --- via build_compute_sweep_groups ---
    # The raw entries above are still listed in build_knowledge_index, but the
    # strategist-facing sweep group refuses the planned roster because one arm
    # lacks a definitive boolean criterion_met verdict.
    groups = await build_compute_sweep_groups(db_session, project.id)
    assert groups == {}


# ── sweep disposition state in compute sweep groups ──────────────────────────


@pytest.mark.asyncio
async def test_compute_sweep_groups_expose_existing_disposition_state(db_session):
    """build_compute_sweep_groups annotates the first arm entry of each sweep
    with sweep_disposition when an act-on-verdict disposition task exists.

    A sweep without a disposition task must NOT have sweep_disposition set.
    A sweep whose disposition task is QUEUED must expose task_id and status.
    A sweep whose disposition task is COMPLETED must expose COMPLETED status.
    """
    from app.knowledge_query import build_compute_sweep_groups

    project, _source_task = await _make_web_research_task(db_session, project_name="DISP")

    sweep_a = "sw-disposition-a"
    sweep_b = "sw-no-disposition"

    # Create compute_experiment tasks and docs for sweep_a (will have a disposition task).
    arm1_task = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id=sweep_a, arm_label="arm1"
    )
    arm2_task = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id=sweep_a, arm_label="arm2"
    )
    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=arm1_task, criterion_met=True
    )
    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=arm2_task, criterion_met=False
    )

    # Create compute_experiment tasks and docs for sweep_b (no disposition task).
    ctrl_task = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id=sweep_b, arm_label="ctrl"
    )
    exp_task = await _make_compute_sweep_task(
        db_session, project_id=project.id, sweep_id=sweep_b, arm_label="exp"
    )
    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=ctrl_task, criterion_met=False
    )
    await _make_compute_sweep_doc(
        db_session, project_id=project.id, task=exp_task, criterion_met=False
    )

    # Spawn a disposition task for sweep_a via context key.
    disposition_task = Task(
        project_id=project.id,
        title=f"act-on-verdict: sweep {sweep_a[:8]}",
        description="Aggregate disposition follow-on",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        payloads=[{"type": "create", "prompt": "act on sweep", "base_branch": "main"}],
        context={"act_on_verdict_sweep_id": sweep_a},
    )
    db_session.add(disposition_task)
    await db_session.commit()
    await db_session.refresh(disposition_task)

    groups = await build_compute_sweep_groups(db_session, project.id)

    assert sweep_a in groups, f"sweep_a must appear in sweep groups; got {list(groups)}"
    assert sweep_b in groups, f"sweep_b must appear in sweep groups; got {list(groups)}"

    # sweep_a: first arm entry must carry sweep_disposition with task_id + status.
    entries_a = groups[sweep_a]
    first_a = entries_a[0]
    assert "sweep_disposition" in first_a, (
        "first arm entry of sweep_a must have sweep_disposition when a disposition task exists"
    )
    disp = first_a["sweep_disposition"]
    assert isinstance(disp, dict)
    assert disp["task_id"] == str(disposition_task.id)
    assert disp["status"] == TaskStatus.QUEUED.value

    # sweep_b: no disposition task — sweep_disposition must be absent.
    entries_b = groups[sweep_b]
    first_b = entries_b[0]
    assert "sweep_disposition" not in first_b, (
        "sweep_b must not have sweep_disposition when no disposition task exists"
    )


# ── _build_research_audit_document: summary signal + title deduplication ─────


class TestBuildResearchAuditDocument:
    """Unit tests for _build_research_audit_document (pure function, no DB)."""

    def _make_task_with_topic(self, topic=None):
        from testing.factories import make_task

        payloads = [{"type": "research"}]
        if topic:
            payloads = [{"type": "research", "topic": topic}]
        return make_task(
            project_id=uuid.uuid4(),
            title="Research task",
            payloads=payloads,
        )

    def test_research_audit_summary_carries_signal(self):
        """Summary includes leading finding titles, not just a bare count."""
        from app.knowledge_completion import _build_research_audit_document

        task = self._make_task_with_topic("context efficiency")
        findings = [
            {"title": "TypeScript migration gap", "description": "desc 1"},
            {"title": "Missing error boundaries", "description": "desc 2"},
            {"title": "Stale test fixtures", "description": "desc 3"},
        ]
        doc = _build_research_audit_document(task, findings, [], None)

        assert doc is not None
        assert "TypeScript migration gap" in doc["summary"]
        assert "Missing error boundaries" in doc["summary"]
        # Should NOT be just a bare count like "3 finding(s) recorded."
        assert "finding(s) recorded" not in doc["summary"]
        # Should include the count
        assert "3" in doc["summary"]

    def test_research_audit_title_no_redundant_prefix(self):
        """When topic falls back to 'Research audit', title is not 'Research audit: Research audit'."""
        from app.knowledge_completion import _build_research_audit_document

        # No topic in payload, no direction title — falls back to "Research audit"
        task = self._make_task_with_topic(topic=None)
        findings = [{"title": "Some finding", "description": "d"}]
        doc = _build_research_audit_document(task, findings, [], None)

        assert doc is not None
        # Title must NOT be the redundant double form
        assert doc["title"] != "Research audit: Research audit"
        # Title should be the clean fallback
        assert doc["title"] == "Research audit"

    def test_research_audit_title_with_real_topic(self):
        """When a real topic is present, the 'Research audit: <topic>' prefix is used."""
        from app.knowledge_completion import _build_research_audit_document

        task = self._make_task_with_topic("context efficiency cuts")
        findings = [{"title": "Finding A", "description": "d"}]
        doc = _build_research_audit_document(task, findings, [], None)

        assert doc is not None
        assert doc["title"] == "Research audit: context efficiency cuts"

    def test_research_audit_summary_fallback_with_no_findings_but_insights(self):
        """When there are only insights (no findings), the summary still carries signal."""
        from app.knowledge_completion import _build_research_audit_document

        task = self._make_task_with_topic("some topic")
        insights = [
            {"summary": "First insight text."},
            {"summary": "Second insight text."},
        ]
        doc = _build_research_audit_document(task, [], insights, None)

        assert doc is not None
        assert "insight(s)" in doc["summary"]
        assert "2" in doc["summary"]

    def test_research_audit_summary_single_finding_title(self):
        """With a single finding, summary contains just that title."""
        from app.knowledge_completion import _build_research_audit_document

        task = self._make_task_with_topic("perf")
        findings = [{"title": "Cache miss on hot path"}]
        doc = _build_research_audit_document(task, findings, [], None)

        assert doc is not None
        assert "Cache miss on hot path" in doc["summary"]
        assert "1" in doc["summary"]

    def test_research_audit_returns_none_when_empty(self):
        """Empty findings and insights yield None (no document to persist)."""
        from app.knowledge_completion import _build_research_audit_document

        task = self._make_task_with_topic("topic")
        assert _build_research_audit_document(task, [], [], None) is None


# ── Knowledge document follow-up lifecycle ──────────────────────────────────


@pytest.mark.asyncio
async def test_pending_followup_auto_set_on_design_completion(
    db_session, _quiet_broadcast
):
    """A design completion persists followup_status='pending' automatically."""
    project, task = await _make_design_task(db_session)

    document = {
        "title": "Design for follow-up lifecycle",
        "summary": "Tracking consumption of knowledge docs.",
        "body": "# Follow-up\n\nDetailed design body.",
        "citations": ["file:docs/design.md"],
        "tags": ["design", "followup"],
        "topic": "follow-up-lifecycle",
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.source_kind == "design"
    assert doc.lifecycle_status == "pending"
    assert doc.consumed_at is None
    assert doc.dismissed_reason is None


@pytest.mark.asyncio
async def test_pending_followup_auto_set_on_web_research_completion(
    db_session, _quiet_broadcast
):
    """A web_research completion persists followup_status='pending' automatically."""
    project, task = await _make_web_research_task(db_session)

    document = {
        "title": "State of X on the web",
        "summary": "A concise survey of X with citations.",
        "body": "# X\n\nDetailed markdown body.",
        "citations": ["https://example.com/x"],
        "tags": ["survey"],
        "topic": "X",
    }
    await _handle_web_research_completion(task, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.source_kind == "web_research"
    assert doc.lifecycle_status == "pending"
    assert doc.consumed_at is None
    assert doc.dismissed_reason is None


@pytest.mark.asyncio
async def test_compute_experiment_completion_leaves_followup_null(
    db_session, _quiet_broadcast
):
    """A compute_experiment completion leaves followup_status as None."""
    from app.knowledge_completion import persist_compute_experiment_knowledge
    from app.models import SourceKind

    project = make_project(name="CEfollowup")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="compute_experiment: bench Y",
        status=TaskStatus.COMPLETED,
        payloads=[
            {"type": "compute_experiment", "experiment": "Y→Z", "base_branch": "main"}
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    doc = await persist_compute_experiment_knowledge(
        task,
        outcome="SUCCESS",
        cost=0.42,
        criterion={
            "metric": "runtime_ms",
            "operator": "<",
            "threshold": 100,
            "value": 80,
            "met": True,
            "measurable": True,
        },
        result_reference="https://storage.example.com/jobs/xyz/result.json",
        session=db_session,
    )
    assert doc is not None
    assert doc.source_kind == SourceKind.COMPUTE_EXPERIMENT.value
    # compute_experiment docs do not participate in the follow-up lifecycle.
    # lifecycle_status inherits the column default ('pending'), but the
    # build_pending_followups query filters by source_kind so they are excluded.
    assert doc.lifecycle_status == "pending"
    assert doc.consumed_at is None
    assert doc.dismissed_at is None
    assert doc.dismissed_reason is None


@pytest.mark.asyncio
async def test_build_pending_followups_returns_only_pending_design_web_research(
    db_session,
):
    """build_pending_followups returns only pending design/web_research docs,
    excluding consumed, dismissed, and other source kinds."""
    from app.knowledge_query import build_pending_followups
    from app.models import SourceKind

    project = make_project(name="PFproject")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create real tasks for FK integrity.
    def _make_doc_task(project_id, title, status=TaskStatus.COMPLETED):
        t = make_task(project_id=project_id, title=title, status=status)
        db_session.add(t)
        return t

    t1 = _make_doc_task(project.id, "Source task A")
    t2 = _make_doc_task(project.id, "Source task B")
    t3 = _make_doc_task(project.id, "Source task C")
    t4 = _make_doc_task(project.id, "Source task D")
    t5 = _make_doc_task(project.id, "Source task E")
    t6 = _make_doc_task(project.id, "Source task F")
    await db_session.commit()
    for t in [t1, t2, t3, t4, t5, t6]:
        await db_session.refresh(t)

    # d1: pending design doc — should appear
    d1 = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t1.id,
        source_kind=SourceKind.DESIGN.value,
        title="Design A",
        summary="First design document.",
        body="# A",
        topic="topic-a",
        tags=["design"],
        lifecycle_status="pending",
    )
    # d2: consumed design doc — should NOT appear
    d2 = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t2.id,
        source_kind=SourceKind.DESIGN.value,
        title="Design B",
        summary="Already consumed design.",
        body="# B",
        topic="topic-b",
        tags=["design"],
        lifecycle_status="consumed",
        consumed_at=_utcnow_func(),
    )
    # d3: pending web_research doc — should appear
    d3 = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t3.id,
        source_kind=SourceKind.WEB_RESEARCH.value,
        title="Research on Z",
        summary="Web research findings.",
        body="# Z",
        topic="z",
        tags=["research"],
        lifecycle_status="pending",
    )
    # d4: dismissed web_research doc — should NOT appear
    d4 = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t4.id,
        source_kind=SourceKind.WEB_RESEARCH.value,
        title="Dismissed research",
        summary="Not actionable.",
        body="# D",
        topic="d",
        tags=["dismissed"],
        lifecycle_status="dismissed",
        dismissed_at=_utcnow_func(),
        dismissed_reason="Out of scope",
    )
    # d5: pending compute_experiment doc — should NOT appear (wrong source kind)
    d5 = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t5.id,
        source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
        title="Benchmark results",
        summary="Compute experiment output.",
        body="# B",
        topic="perf",
        tags=["compute"],
        lifecycle_status="pending",
    )
    # d6: consumed design doc — should NOT appear
    # (lifecycle_status is NOT NULL with default='pending', so the old
    # followup_status=None case is impossible in the new model.)
    d6 = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t6.id,
        source_kind=SourceKind.DESIGN.value,
        title="Legacy design",
        summary="Consumed design.",
        body="# L",
        topic="legacy",
        tags=["legacy"],
        lifecycle_status="consumed",
        consumed_at=_utcnow_func(),
    )

    for d in [d1, d2, d3, d4, d5, d6]:
        db_session.add(d)
    await db_session.commit()

    entries = await build_pending_followups(db_session, project.id)
    ids = {e["id"] for e in entries}
    assert str(d1.id) in ids
    assert str(d3.id) in ids
    assert str(d2.id) not in ids  # consumed
    assert str(d4.id) not in ids  # dismissed
    assert str(d5.id) not in ids  # compute_experiment
    assert str(d6.id) not in ids  # consumed


@pytest.mark.asyncio
async def test_consume_knowledge_followup_op(client: AsyncClient, db_session):
    """The consume_knowledge_followup strategic op transitions status to 'consumed'."""
    from app.auth import create_agent_token
    from app.models import SourceKind

    project = make_project(name="consumeop")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Source task for consume op",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=src_task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Consumable design",
        summary="Ready to consume.",
        body="# C",
        topic="test",
        tags=["test"],
        lifecycle_status="pending",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    task = make_task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-consume",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-consume", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "consume_knowledge_followup"
    assert body["lifecycle_status"] == "consumed"
    assert body["document_id"] == str(doc.id)

    await db_session.refresh(doc)
    assert doc.lifecycle_status == "consumed"
    assert doc.consumed_at is not None


@pytest.mark.asyncio
async def test_dismiss_knowledge_followup_op(client: AsyncClient, db_session):
    """The dismiss_knowledge_followup strategic op transitions status to 'dismissed'."""
    from app.auth import create_agent_token
    from app.models import SourceKind

    project = make_project(name="dismissop")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Source task for dismiss op",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=src_task.id,
        source_kind=SourceKind.WEB_RESEARCH.value,
        title="Dismissible research",
        summary="Not actionable right now.",
        body="# D",
        topic="test",
        tags=["test"],
        lifecycle_status="pending",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    task = make_task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-dismiss",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dismiss", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
                "reason": "Out of scope for current cycle",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "dismiss_knowledge_followup"
    assert body["lifecycle_status"] == "dismissed"
    assert body["document_id"] == str(doc.id)

    await db_session.refresh(doc)
    assert doc.lifecycle_status == "dismissed"
    assert doc.dismissed_at is not None
    assert "Out of scope" in doc.dismissed_reason


@pytest.mark.asyncio
async def test_consume_already_consumed_returns_409(client: AsyncClient, db_session):
    """Consuming a document that is already consumed returns 409."""
    from app.auth import create_agent_token
    from app.models import SourceKind

    project = make_project(name="consume409")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Source task for consume 409",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=src_task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Already consumed",
        summary="Was consumed earlier.",
        body="# A",
        topic="test",
        tags=["test"],
        lifecycle_status="consumed",
        consumed_at=_utcnow_func(),
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    task = make_task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-consume409",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-consume409", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
                "reason": "Trying to consume again",
            },
        },
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_dismiss_already_dismissed_returns_409(client: AsyncClient, db_session):
    """Dismissing a document that is already dismissed returns 409."""
    from app.auth import create_agent_token
    from app.models import SourceKind

    project = make_project(name="dismiss409")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Source task for dismiss 409",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=src_task.id,
        source_kind=SourceKind.WEB_RESEARCH.value,
        title="Already dismissed",
        summary="Was dismissed earlier.",
        body="# A",
        topic="test",
        tags=["test"],
        lifecycle_status="dismissed",
        dismissed_at=_utcnow_func(),
        dismissed_reason="Not relevant",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    task = make_task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-dismiss409",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dismiss409", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
                "reason": "Trying to dismiss again",
            },
        },
    )
    assert resp.status_code == 409, resp.text


# ── lifecycle ops: consume_knowledge / dismiss_knowledge ──────────────────────


@pytest.fixture
async def _lifecycle_setup(db_session):
    """Create project + strategic review task + pending knowledge doc for lifecycle tests."""
    from app.auth import create_agent_token

    project = make_project(name="Lifecycle Test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Strategic review for lifecycle",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="vm-lifecycle-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    doc = KnowledgeDocument(
        id=uuid.uuid4(),
        project_id=project.id,
        source_task_id=task.id,
        source_kind="design",
        title="Test Design Document",
        summary="A test design doc awaiting consumption",
        body="# Test Design\n\nThis is a test.",
        topic="testing",
        forward_quality_verdict={},
        lifecycle_status="pending",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    token = create_agent_token(str(task.id), "vm-lifecycle-test", task_type="strategic_review")
    return project, task, doc, token


@pytest.mark.asyncio
async def test_consume_knowledge_transitions_pending_to_consumed(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """consume_knowledge sets lifecycle_status=consumed and consumed_at timestamp."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge",
            "data": {"knowledge_document_id": str(doc.id)},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "consume_knowledge"
    assert body["document_id"] == str(doc.id)

    # Verify durable DB state
    await db_session.refresh(doc)
    assert doc.lifecycle_status == "consumed"
    assert doc.consumed_at is not None


@pytest.mark.asyncio
async def test_consume_knowledge_404_on_wrong_project(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """consume_knowledge returns 404 when the document belongs to a different project."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    # Use a random UUID that doesn't exist in this project
    unknown_id = uuid.uuid4()
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge",
            "data": {"knowledge_document_id": str(unknown_id)},
        },
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_dismiss_knowledge_transitions_pending_to_dismissed(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """dismiss_knowledge sets lifecycle_status=dismissed with timestamp and reason."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge",
            "data": {
                "knowledge_document_id": str(doc.id),
                "reason": "Design is already covered by direction d-abc123",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "dismiss_knowledge"
    assert body["document_id"] == str(doc.id)

    # Verify durable DB state
    await db_session.refresh(doc)
    assert doc.lifecycle_status == "dismissed"
    assert doc.dismissed_at is not None
    assert doc.dismissed_reason == "Design is already covered by direction d-abc123"


@pytest.mark.asyncio
async def test_dismiss_knowledge_without_reason(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """dismiss_knowledge works without an optional reason."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge",
            "data": {"knowledge_document_id": str(doc.id)},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"

    await db_session.refresh(doc)
    assert doc.lifecycle_status == "dismissed"
    assert doc.dismissed_at is not None
    assert doc.dismissed_reason is None


@pytest.mark.asyncio
async def test_dismiss_knowledge_404_on_missing_doc(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """dismiss_knowledge returns 404 when the document does not exist."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    unknown_id = uuid.uuid4()
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge",
            "data": {"knowledge_document_id": str(unknown_id)},
        },
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_consume_knowledge_rejects_malformed_payload(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """consume_knowledge returns 400 when data is not a dict with knowledge_document_id."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    # Missing knowledge_document_id entirely
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge",
            "data": {},
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text

    # Invalid UUID
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge",
            "data": {"knowledge_document_id": "not-a-uuid"},
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_dismiss_knowledge_rejects_malformed_payload(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """dismiss_knowledge returns 400 when data is not a dict with knowledge_document_id."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    # Missing knowledge_document_id entirely
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge",
            "data": {},
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text

    # Invalid UUID
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge",
            "data": {"knowledge_document_id": "not-a-uuid"},
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_ops_set_strategic_api_called_on_task_context(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """Both consume_knowledge and dismiss_knowledge set _strategic_api_called on task context."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge",
            "data": {"knowledge_document_id": str(doc.id)},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_api_called") is True


@pytest.mark.asyncio
async def test_lifecycle_status_query_filter_works(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """The lifecycle_status query param filters the list endpoint correctly."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    # Doc starts as "pending" — it should appear when filtering for pending
    pending_resp = await client.get(
        "/api/agent/knowledge-documents?lifecycle_status=pending",
        headers=headers,
    )
    assert pending_resp.status_code == 200, pending_resp.text
    pending_ids = [d["id"] for d in pending_resp.json()["documents"]]
    assert str(doc.id) in pending_ids

    # It should NOT appear when filtering for "consumed"
    consumed_resp = await client.get(
        "/api/agent/knowledge-documents?lifecycle_status=consumed",
        headers=headers,
    )
    assert consumed_resp.status_code == 200, consumed_resp.text
    consumed_ids = [d["id"] for d in consumed_resp.json()["documents"]]
    assert str(doc.id) not in consumed_ids

    # After consuming the doc, it should appear in the "consumed" filter
    await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge",
            "data": {"knowledge_document_id": str(doc.id)},
        },
        headers=headers,
    )
    consumed_after = await client.get(
        "/api/agent/knowledge-documents?lifecycle_status=consumed",
        headers=headers,
    )
    assert consumed_after.status_code == 200
    consumed_after_ids = [d["id"] for d in consumed_after.json()["documents"]]
    assert str(doc.id) in consumed_after_ids


# ── build_pending_followups: additional edge-case coverage ───────────────────


@pytest.mark.asyncio
async def test_build_pending_followups_cross_project_isolation(db_session):
    """Pending docs from a different project must NOT appear in another project's followups."""
    from app.knowledge_query import build_pending_followups
    from app.models import SourceKind

    project_a = make_project(name="BPF-cross-A")
    project_b = make_project(name="BPF-cross-B")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = make_task(
        project_id=project_a.id,
        title="task in project A",
        status=TaskStatus.COMPLETED,
    )
    task_b = make_task(
        project_id=project_b.id,
        title="task in project B",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    # A pending design doc owned by project B
    doc_b = KnowledgeDocument(
        project_id=project_b.id,
        source_task_id=task_b.id,
        source_kind=SourceKind.DESIGN.value,
        title="Project B Design",
        summary="Belongs to project B.",
        body="# B",
        lifecycle_status="pending",
    )
    # A pending web_research doc owned by project A
    doc_a = KnowledgeDocument(
        project_id=project_a.id,
        source_task_id=task_a.id,
        source_kind=SourceKind.WEB_RESEARCH.value,
        title="Project A Research",
        summary="Belongs to project A.",
        body="# A",
        lifecycle_status="pending",
    )
    db_session.add(doc_a)
    db_session.add(doc_b)
    await db_session.commit()

    entries_a = await build_pending_followups(db_session, project_a.id)
    ids_a = {e["id"] for e in entries_a}
    assert str(doc_a.id) in ids_a
    assert str(doc_b.id) not in ids_a, "doc from project B must NOT appear in project A's followups"

    entries_b = await build_pending_followups(db_session, project_b.id)
    ids_b = {e["id"] for e in entries_b}
    assert str(doc_b.id) in ids_b
    assert str(doc_a.id) not in ids_b, "doc from project A must NOT appear in project B's followups"


@pytest.mark.asyncio
async def test_build_pending_followups_oldest_first_ordering(db_session):
    """Pending followups are returned oldest-first by created_at so stale items surface first."""
    from datetime import datetime, timedelta

    from app.knowledge_query import build_pending_followups
    from app.models import SourceKind

    project = make_project(name="BPF-order")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = make_task(project_id=project.id, title="task 1", status=TaskStatus.COMPLETED)
    t2 = make_task(project_id=project.id, title="task 2", status=TaskStatus.COMPLETED)
    t3 = make_task(project_id=project.id, title="task 3", status=TaskStatus.COMPLETED)
    db_session.add(t1)
    db_session.add(t2)
    db_session.add(t3)
    await db_session.commit()
    await db_session.refresh(t1)
    await db_session.refresh(t2)
    await db_session.refresh(t3)

    base = datetime(2026, 3, 1, 10, 0, 0)
    d_old = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t1.id,
        source_kind=SourceKind.DESIGN.value,
        title="Oldest doc",
        summary="Oldest",
        body="# old",
        lifecycle_status="pending",
        created_at=base,
    )
    d_mid = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t2.id,
        source_kind=SourceKind.WEB_RESEARCH.value,
        title="Middle doc",
        summary="Middle",
        body="# mid",
        lifecycle_status="pending",
        created_at=base + timedelta(hours=1),
    )
    d_new = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t3.id,
        source_kind=SourceKind.DESIGN.value,
        title="Newest doc",
        summary="Newest",
        body="# new",
        lifecycle_status="pending",
        created_at=base + timedelta(hours=2),
    )
    db_session.add(d_old)
    db_session.add(d_mid)
    db_session.add(d_new)
    await db_session.commit()

    entries = await build_pending_followups(db_session, project.id)
    titles = [e["title"] for e in entries]
    assert titles == ["Oldest doc", "Middle doc", "Newest doc"]


@pytest.mark.asyncio
async def test_build_pending_followups_summary_truncation(db_session):
    """Summaries longer than the cap are collapsed (whitespace) and truncated with '…'."""
    from app.knowledge_query import (
        _PENDING_LIFECYCLE_ITEMS_SUMMARY_CHARS,
        build_pending_followups,
    )
    from app.models import SourceKind

    project = make_project(name="BPF-trunc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id, title="trunc task", status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Build a summary longer than the cap; pad with spaces so whitespace collapse doesn't help
    long_summary = "word " * 200  # ~1000 chars when collapsed
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Design with long summary",
        summary=long_summary,
        body="# Body",
        lifecycle_status="pending",
    )
    db_session.add(doc)
    await db_session.commit()

    entries = await build_pending_followups(db_session, project.id)
    assert len(entries) == 1
    summary = entries[0]["summary"]
    # Must end with the ellipsis sentinel
    assert summary.endswith("…"), f"Expected truncated summary ending with '…', got: {summary!r}"
    # Must not exceed the cap
    assert len(summary) <= _PENDING_LIFECYCLE_ITEMS_SUMMARY_CHARS, (
        f"Truncated summary length {len(summary)} exceeds cap {_PENDING_LIFECYCLE_ITEMS_SUMMARY_CHARS}"
    )


@pytest.mark.asyncio
async def test_build_pending_followups_respects_limit_cap(db_session):
    """build_pending_followups returns at most `limit` entries."""
    from app.knowledge_query import build_pending_followups
    from app.models import SourceKind

    project = make_project(name="BPF-limit")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create 5 tasks + docs
    tasks = []
    for i in range(5):
        t = make_task(project_id=project.id, title=f"limit task {i}", status=TaskStatus.COMPLETED)
        db_session.add(t)
        tasks.append(t)
    await db_session.commit()
    for t in tasks:
        await db_session.refresh(t)

    for i, t in enumerate(tasks):
        d = KnowledgeDocument(
            project_id=project.id,
            source_task_id=t.id,
            source_kind=SourceKind.DESIGN.value,
            title=f"Doc {i}",
            summary=f"Summary {i}",
            body=f"# {i}",
            lifecycle_status="pending",
        )
        db_session.add(d)
    await db_session.commit()

    # Default limit (20) returns all 5
    entries_all = await build_pending_followups(db_session, project.id)
    assert len(entries_all) == 5

    # Custom limit of 2 returns at most 2
    entries_limited = await build_pending_followups(db_session, project.id, limit=2)
    assert len(entries_limited) == 2


@pytest.mark.asyncio
async def test_build_pending_followups_entry_fields_shape(db_session):
    """Each entry carries id, title, summary, topic, source_kind, direction_id, created_at, and tags."""
    from app.knowledge_query import build_pending_followups
    from app.models import SourceKind

    project = make_project(name="BPF-shape")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id, title="shape task", status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        source_kind=SourceKind.WEB_RESEARCH.value,
        title="Field-shape check",
        summary="Summary for shape check.",
        body="# Body",
        topic="shape-topic",
        direction_id="d-aabbccdd",
        tags=["tag1", "tag2"],
        lifecycle_status="pending",
    )
    db_session.add(doc)
    await db_session.commit()

    entries = await build_pending_followups(db_session, project.id)
    assert len(entries) == 1
    e = entries[0]

    assert e["id"] == str(doc.id)
    assert e["title"] == "Field-shape check"
    assert e["summary"] == "Summary for shape check."
    assert e["topic"] == "shape-topic"
    assert e["source_kind"] == SourceKind.WEB_RESEARCH.value
    assert e["direction_id"] == "d-aabbccdd"
    assert e["tags"] == ["tag1", "tag2"]
    assert e["created_at"] is not None  # ISO timestamp string


@pytest.mark.asyncio
async def test_build_pending_followups_excludes_research_audit(db_session):
    """research_audit docs are excluded from the follow-up lifecycle (only design + web_research appear)."""
    from app.knowledge_query import build_pending_followups
    from app.models import SourceKind

    project = make_project(name="BPF-audit")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t_audit = make_task(project_id=project.id, title="audit task", status=TaskStatus.COMPLETED)
    t_design = make_task(project_id=project.id, title="design task", status=TaskStatus.COMPLETED)
    db_session.add(t_audit)
    db_session.add(t_design)
    await db_session.commit()
    await db_session.refresh(t_audit)
    await db_session.refresh(t_design)

    doc_audit = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t_audit.id,
        source_kind=SourceKind.RESEARCH_AUDIT.value,
        title="Research audit doc",
        summary="Audit output.",
        body="# Audit",
        lifecycle_status="pending",
    )
    doc_design = KnowledgeDocument(
        project_id=project.id,
        source_task_id=t_design.id,
        source_kind=SourceKind.DESIGN.value,
        title="Regular design doc",
        summary="Design output.",
        body="# Design",
        lifecycle_status="pending",
    )
    db_session.add(doc_audit)
    db_session.add(doc_design)
    await db_session.commit()

    entries = await build_pending_followups(db_session, project.id)
    ids = {e["id"] for e in entries}
    assert str(doc_design.id) in ids, "design doc should appear"
    assert str(doc_audit.id) not in ids, "research_audit doc must NOT appear in lifecycle followups"


# ── lifecycle ops: null lifecycle_status edge cases ──────────────────────


async def _make_null_lifecycle_doc(db_session, project, task):
    """Create a KnowledgeDocument then force lifecycle_status=NULL via raw SQL.

    The ORM model defines lifecycle_status as NOT NULL with a default of
    'pending', which prevents setting None through the ORM. Using raw SQL
    bypasses the default and simulates legacy docs created before migration
    0446 or via out-of-band code paths that bypass the ORM default.

    Returns the doc with lifecycle_status set to ``None``, or marks the test
    as skipped if the database driver enforces NOT NULL (PostgreSQL with a
    fully-applied migration) and refuses the NULL assignment.
    """
    import sqlalchemy.exc
    from app.models import SourceKind
    from sqlalchemy import text

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Null lifecycle doc",
        summary="A doc with null lifecycle_status.",
        body="# Null lifecycle",
        lifecycle_status="pending",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    try:
        await db_session.execute(
            text("UPDATE knowledgedocument SET lifecycle_status = NULL WHERE id = :id"),
            {"id": doc.id},
        )
        await db_session.commit()
        await db_session.refresh(doc)
    except sqlalchemy.exc.IntegrityError:
        pytest.skip(
            "Database refuses NULL lifecycle_status (NOT NULL constraint). "
            "Production docs with null lifecycle_status exist due to an "
            "outstanding schema gap; this test exercises behavior in "
            "environments that can reproduce that state."
        )
        return None  # unreachable
    return doc


@pytest.mark.asyncio
async def test_dismiss_knowledge_followup_null_lifecycle_returns_409(
    client: AsyncClient, db_session,
):
    """dismiss_knowledge_followup returns 409 when lifecycle_status is null.

    Legacy KnowledgeDocuments created before lifecycle_status was enforced
    (migration 0446) may have lifecycle_status=NULL. The followup dismiss op
    requires exactly 'pending' and must reject null explicitly.
    """
    from app.auth import create_agent_token

    project = make_project(name="null-lifecycle-dismiss")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Src for null lifecycle dismiss",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = await _make_null_lifecycle_doc(db_session, project, src_task)

    task = make_task(
        project_id=project.id,
        title="Strategic review for null lifecycle",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="vm-null-lifecycle-dismiss",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-null-lifecycle-dismiss", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
                "reason": "Test dismiss of null lifecycle doc",
            },
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert "lifecycle_status" in body.get("detail", "")


@pytest.mark.asyncio
async def test_consume_knowledge_followup_null_lifecycle_returns_409(
    client: AsyncClient, db_session,
):
    """consume_knowledge_followup returns 409 when lifecycle_status is null."""
    from app.auth import create_agent_token

    project = make_project(name="null-lifecycle-consume")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Src for null lifecycle consume",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = await _make_null_lifecycle_doc(db_session, project, src_task)

    task = make_task(
        project_id=project.id,
        title="Strategic review for null lifecycle consume",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="vm-null-lifecycle-consume",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-null-lifecycle-consume", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
            },
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert "lifecycle_status" in body.get("detail", "")


@pytest.mark.asyncio
async def test_dismiss_knowledge_followup_consumed_returns_409(
    client: AsyncClient, db_session,
):
    """dismiss_knowledge_followup returns 409 when lifecycle_status is already 'consumed'."""
    from app.auth import create_agent_token
    from app.models import SourceKind

    project = make_project(name="dismiss-consumed")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Src for dismiss of consumed",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=src_task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Consumed doc",
        summary="Already consumed doc.",
        body="# Consumed",
        lifecycle_status="consumed",
        consumed_at=_utcnow_func(),
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    task = make_task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="vm-dismiss-consumed",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-dismiss-consumed", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
                "reason": "Try dismiss consumed doc",
            },
        },
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_consume_knowledge_followup_dismissed_returns_409(
    client: AsyncClient, db_session,
):
    """consume_knowledge_followup returns 409 when lifecycle_status is already 'dismissed'."""
    from app.auth import create_agent_token
    from app.models import SourceKind

    project = make_project(name="consume-dismissed")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Src for consume of dismissed",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=src_task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Dismissed doc",
        summary="Already dismissed doc.",
        body="# Dismissed",
        lifecycle_status="dismissed",
        dismissed_at=_utcnow_func(),
        dismissed_reason="Not relevant",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    task = make_task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="vm-consume-dismissed",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-consume-dismissed", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
            },
        },
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_dismiss_knowledge_followup_missing_reason_returns_400(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """dismiss_knowledge_followup returns 400 when reason is empty or missing."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    # Empty reason
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": {
                "document_id": str(doc.id),
                "reason": "",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert "reason" in resp.text.lower()

    # Missing reason key entirely
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": {"document_id": str(doc.id)},
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert "reason" in resp.text.lower()


@pytest.mark.asyncio
async def test_dismiss_knowledge_followup_not_found_returns_404(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """dismiss_knowledge_followup returns 404 when the document does not exist."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    unknown_id = uuid.uuid4()
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": {
                "document_id": str(unknown_id),
                "reason": "Doc does not exist",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_consume_knowledge_followup_not_found_returns_404(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """consume_knowledge_followup returns 404 when the document does not exist."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    unknown_id = uuid.uuid4()
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge_followup",
            "data": {"document_id": str(unknown_id)},
        },
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_dismiss_knowledge_followup_data_not_dict_returns_400(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """dismiss_knowledge_followup returns 400 when data is not a dict."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge_followup",
            "data": "not-a-dict",
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_consume_knowledge_followup_data_not_dict_returns_400(
    client: AsyncClient, db_session, _lifecycle_setup
):
    """consume_knowledge_followup returns 400 when data is not a dict."""
    project, task, doc, token = _lifecycle_setup
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge_followup",
            "data": "not-a-dict",
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_dismiss_knowledge_null_lifecycle_succeeds(
    client: AsyncClient, db_session,
):
    """dismiss_knowledge (non-followup variant) works on null lifecycle_status docs.

    Unlike dismiss_knowledge_followup, the simpler dismiss op does not require
    lifecycle_status to be 'pending' — it directly sets it to 'dismissed'. This
    is the correct escape hatch for legacy docs with null lifecycle_status.
    """
    from app.auth import create_agent_token

    project = make_project(name="null-dismiss-op")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Src for null dismiss op",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = await _make_null_lifecycle_doc(db_session, project, src_task)

    task = make_task(
        project_id=project.id,
        title="Strategic review for null dismiss op",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="vm-null-dismiss-op",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-null-dismiss-op", task_type="strategic_review")
    reason = "Superseded by later design"
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "dismiss_knowledge",
            "data": {
                "knowledge_document_id": str(doc.id),
                "reason": reason,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "dismiss_knowledge"

    await db_session.refresh(doc)
    assert doc.lifecycle_status == "dismissed"
    assert doc.dismissed_at is not None
    assert doc.dismissed_reason == reason


@pytest.mark.asyncio
async def test_consume_knowledge_null_lifecycle_succeeds(
    client: AsyncClient, db_session,
):
    """consume_knowledge (non-followup variant) works on null lifecycle_status docs."""
    from app.auth import create_agent_token

    project = make_project(name="null-consume-op")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src_task = make_task(
        project_id=project.id,
        title="Src for null consume op",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    doc = await _make_null_lifecycle_doc(db_session, project, src_task)

    task = make_task(
        project_id=project.id,
        title="Strategic review for null consume op",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="vm-null-consume-op",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-null-consume-op", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "consume_knowledge",
            "data": {"knowledge_document_id": str(doc.id)},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "consume_knowledge"

    await db_session.refresh(doc)
    assert doc.lifecycle_status == "consumed"
    assert doc.consumed_at is not None

# ── strategic context evidence ordering (oldest pending first) ─────────


@pytest.mark.asyncio
async def test_strategic_context_evidence_orders_by_oldest_pending_first(db_session):
    """load_strategic_context_evidence returns pending knowledge docs oldest-first
    so stale items are never displaced by newer ones within the 4-item limit."""
    from datetime import datetime, timedelta

    from app.strategic_context_package import load_strategic_context_evidence

    project = make_project(name="SCEvidenceOrder")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    base = datetime(2026, 1, 15, 12, 0, 0)
    # Create 5 pending docs with staggered ages (oldest first in iteration).
    titles = []
    for i in range(5):
        title = f"Pending doc {i}"
        titles.append(title)
        doc = KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title=title,
            summary=f"Summary {i}.",
            body=f"# Body {i}\n\nContent.",
            topic="t",
            source_kind="web_research",
            lifecycle_status="pending",
            created_at=base + timedelta(hours=i),
            updated_at=base + timedelta(hours=i),
        )
        db_session.add(doc)
    await db_session.commit()

    evidence = await load_strategic_context_evidence(db_session, project.id)
    knowledge = evidence["knowledge"]
    assert knowledge["total"] == 5
    assert len(knowledge["items"]) == 4  # .limit(4) in effect
    assert knowledge["pending_count"] == 5  # aggregate count(*) of ALL pending docs in the project

    # Items must be ordered oldest-first (by created_at).
    timestamps = [item["created_at"] for item in knowledge["items"]]
    assert timestamps == sorted(timestamps), (
        "pending knowledge items must be ordered oldest-first"
    )

    # The oldest 4 titles should be the returned items.
    returned_titles = [item["label"] for item in knowledge["items"]]
    assert returned_titles == titles[:4], (
        f"expected oldest 4 docs {titles[:4]}, got {returned_titles}"
    )


# ── knowledge commission dedup guards ────────────────────────────────────────
# Tests for knowledge_doc_recently_produced and knowledge_task_in_flight in
# app/knowledge_query.py.  These guards prevent redundant web_research/design
# commissions from _handle_research_completion when the same direction+topic
# knowledge has recently been produced or is already in flight.


async def _make_project_and_task(
    db_session,
    *,
    project_name="KGUARD",
    task_type="web_research",
    topic=None,
    direction_id=None,
    status=TaskStatus.COMPLETED,
):
    """Minimal project + task for guard tests."""
    project = make_project(name=project_name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = {"type": task_type, "base_branch": "main"}
    if topic is not None:
        payload["topic"] = topic
    if direction_id is not None:
        payload["direction_id"] = direction_id

    task = make_task(
        project_id=project.id,
        title=f"{task_type}: {topic or 'X'}",
        status=status,
        payloads=[payload],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


async def _make_knowledge_doc_for_guard(
    db_session,
    project_id,
    source_task_id,
    *,
    topic="some-topic",
    direction_id=None,
    source_kind="web_research",
    age_hours=0,
):
    """Insert a KnowledgeDocument for guard tests with controllable age/direction."""
    from datetime import timedelta

    created_at = _utcnow_func() - timedelta(hours=age_hours)
    doc = KnowledgeDocument(
        project_id=project_id,
        source_task_id=source_task_id,
        title=f"Doc on {topic}",
        summary="Summary.",
        body="Body.",
        topic=topic,
        direction_id=direction_id,
        source_kind=source_kind,
        tags=[],
        source_citations=[],
        forward_quality_verdict={"passed": True, "strategy": "forward-quality"},
        created_at=created_at,
        updated_at=created_at,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


# ── knowledge_doc_recently_produced ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_kguard_doc_recent_same_direction_topic_returns_true(db_session):
    """knowledge_doc_recently_produced → True when doc for same direction+topic exists within cooldown."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocHit", topic="auth-flow", direction_id="d-001"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="auth-flow", direction_id="d-001", age_hours=1
    )

    result = await knowledge_doc_recently_produced(
        db_session, project.id, "d-001", "auth-flow"
    )
    assert result is True


@pytest.mark.asyncio
async def test_kguard_doc_different_direction_returns_false(db_session):
    """knowledge_doc_recently_produced → False when direction differs (no cross-direction false positive)."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocDirMiss", topic="auth-flow", direction_id="d-001"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="auth-flow", direction_id="d-001", age_hours=1
    )

    result = await knowledge_doc_recently_produced(
        db_session, project.id, "d-DIFFERENT", "auth-flow"
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_doc_different_topic_returns_false(db_session):
    """knowledge_doc_recently_produced → False when topic differs (no same-direction false positive)."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocTopicMiss", topic="auth-flow", direction_id="d-001"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="auth-flow", direction_id="d-001", age_hours=1
    )

    result = await knowledge_doc_recently_produced(
        db_session, project.id, "d-001", "totally-different-topic"
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_doc_outside_cooldown_returns_false(db_session):
    """knowledge_doc_recently_produced → False when doc is older than the cooldown window."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocOld", topic="caching", direction_id="d-002"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="caching", direction_id="d-002",
        age_hours=50  # older than 48h default cooldown
    )

    result = await knowledge_doc_recently_produced(
        db_session, project.id, "d-002", "caching"
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_doc_absent_direction_id_returns_false(db_session):
    """knowledge_doc_recently_produced → False when direction_id is None (no reliable dedup basis)."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocNoDirID", topic="caching", direction_id="d-003"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="caching", direction_id="d-003", age_hours=1
    )

    result = await knowledge_doc_recently_produced(
        db_session, project.id, None, "caching"
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_doc_absent_topic_returns_false(db_session):
    """knowledge_doc_recently_produced → False when topic is None (no reliable dedup basis)."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocNoTopic", topic="caching", direction_id="d-003"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="caching", direction_id="d-003", age_hours=1
    )

    result = await knowledge_doc_recently_produced(
        db_session, project.id, "d-003", None
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_doc_zero_cooldown_disables_guard(db_session):
    """knowledge_doc_recently_produced → False when cooldown_hours=0 (guard disabled by operator)."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocZeroCooldown", topic="routing", direction_id="d-004"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="routing", direction_id="d-004", age_hours=1
    )

    result = await knowledge_doc_recently_produced(
        db_session, project.id, "d-004", "routing", cooldown_hours=0
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_doc_custom_cooldown_hours(db_session):
    """knowledge_doc_recently_produced respects a custom cooldown_hours parameter."""
    from app.knowledge_query import knowledge_doc_recently_produced

    project, task = await _make_project_and_task(
        db_session, project_name="KGdocCustomCooldown", topic="routing", direction_id="d-005"
    )
    await _make_knowledge_doc_for_guard(
        db_session, project.id, task.id, topic="routing", direction_id="d-005",
        age_hours=6  # 6 hours old
    )

    # With 4h cooldown: doc is outside window → False
    result_outside = await knowledge_doc_recently_produced(
        db_session, project.id, "d-005", "routing", cooldown_hours=4
    )
    assert result_outside is False

    # With 24h cooldown: doc is inside window → True
    result_inside = await knowledge_doc_recently_produced(
        db_session, project.id, "d-005", "routing", cooldown_hours=24
    )
    assert result_inside is True


# ── knowledge_task_in_flight ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kguard_inflight_active_task_same_direction_topic_returns_true(db_session):
    """knowledge_task_in_flight → True when an IN_PROGRESS task for same direction+topic exists."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflight",
        task_type="web_research",
        topic="perf-baseline",
        direction_id="d-inf-001",
        status=TaskStatus.IN_PROGRESS,
    )

    result = await knowledge_task_in_flight(
        db_session, project.id, "d-inf-001", "perf-baseline"
    )
    assert result is True


@pytest.mark.asyncio
async def test_kguard_inflight_queued_task_returns_true(db_session):
    """knowledge_task_in_flight → True when a QUEUED task for same direction+topic exists."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflightQ",
        task_type="web_research",
        topic="perf-baseline",
        direction_id="d-inf-002",
        status=TaskStatus.QUEUED,
    )

    result = await knowledge_task_in_flight(
        db_session, project.id, "d-inf-002", "perf-baseline"
    )
    assert result is True


@pytest.mark.asyncio
async def test_kguard_inflight_different_direction_returns_false(db_session):
    """knowledge_task_in_flight → False when direction differs (no cross-direction false positive)."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflightDirMiss",
        task_type="web_research",
        topic="perf-baseline",
        direction_id="d-inf-001",
        status=TaskStatus.IN_PROGRESS,
    )

    result = await knowledge_task_in_flight(
        db_session, project.id, "d-DIFFERENT", "perf-baseline"
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_inflight_different_topic_returns_false(db_session):
    """knowledge_task_in_flight → False when topic differs (distinct topics are not deduplicated)."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflightTopicMiss",
        task_type="web_research",
        topic="perf-baseline",
        direction_id="d-inf-001",
        status=TaskStatus.IN_PROGRESS,
    )

    result = await knowledge_task_in_flight(
        db_session, project.id, "d-inf-001", "completely-different-topic"
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_inflight_completed_task_not_in_flight(db_session):
    """knowledge_task_in_flight → False for COMPLETED tasks (only active tasks count)."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflightDone",
        task_type="web_research",
        topic="completed-research",
        direction_id="d-inf-003",
        status=TaskStatus.COMPLETED,
    )

    result = await knowledge_task_in_flight(
        db_session, project.id, "d-inf-003", "completed-research"
    )
    assert result is False


@pytest.mark.asyncio
async def test_kguard_inflight_absent_direction_id_returns_false(db_session):
    """knowledge_task_in_flight → False when direction_id is None."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflightNoDirID",
        task_type="web_research",
        topic="auth",
        direction_id="d-inf-004",
        status=TaskStatus.IN_PROGRESS,
    )

    result = await knowledge_task_in_flight(db_session, project.id, None, "auth")
    assert result is False


@pytest.mark.asyncio
async def test_kguard_inflight_absent_topic_returns_false(db_session):
    """knowledge_task_in_flight → False when topic is None."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflightNoTopic",
        task_type="web_research",
        topic="auth",
        direction_id="d-inf-004",
        status=TaskStatus.IN_PROGRESS,
    )

    result = await knowledge_task_in_flight(db_session, project.id, "d-inf-004", None)
    assert result is False


@pytest.mark.asyncio
async def test_kguard_inflight_design_task_detected(db_session):
    """knowledge_task_in_flight → True for an active design task (not just web_research)."""
    from app.knowledge_query import knowledge_task_in_flight

    project, _ = await _make_project_and_task(
        db_session,
        project_name="KGinflightDesign",
        task_type="design",
        topic="api-design",
        direction_id="d-inf-005",
        status=TaskStatus.IN_PROGRESS,
    )

    result = await knowledge_task_in_flight(
        db_session, project.id, "d-inf-005", "api-design"
    )
    assert result is True


@pytest.mark.asyncio
async def test_kguard_distinct_topics_same_direction_not_deduplicated(db_session):
    """Two distinct topics under the same direction are NOT deduplicated against each other."""
    from app.knowledge_query import knowledge_task_in_flight, knowledge_doc_recently_produced

    # Create in-flight task for topic-A in direction d-006
    project_a, _ = await _make_project_and_task(
        db_session,
        project_name="KGdistinctTopics",
        task_type="web_research",
        topic="topic-alpha",
        direction_id="d-006",
        status=TaskStatus.IN_PROGRESS,
    )

    # topic-beta under same direction should NOT be blocked
    result = await knowledge_task_in_flight(
        db_session, project_a.id, "d-006", "topic-beta"
    )
    assert result is False, "Distinct topics must not be deduplicated against each other"

    # Also verify the doc guard doesn't cross topics
    task2 = make_task(
        project_id=project_a.id,
        title="WR: topic-alpha completed",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "web_research", "topic": "topic-alpha", "base_branch": "main"}],
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)
    await _make_knowledge_doc_for_guard(
        db_session, project_a.id, task2.id,
        topic="topic-alpha", direction_id="d-006", age_hours=1
    )

    doc_result = await knowledge_doc_recently_produced(
        db_session, project_a.id, "d-006", "topic-beta"
    )
    assert doc_result is False, "Recent doc for topic-alpha must not block commission for topic-beta"


# ── greenfield web-only design citation gate (end-to-end) ────────────────────
#
# A "greenfield" design document cites ONLY web URLs (no file-path citations)
# because the target repo is empty or has no relevant codebase to reference.
# The backend runs ``_design_web_only_grounding_miss`` which probes each URL for
# reachability so a doc grounded entirely in dead/fabricated links cannot pass.
# The gate errs toward acceptance — any reachable URL or all-transient errors
# accept; ALL definitively dead URLs reject.


@pytest.mark.asyncio
async def test_greenfield_web_only_design_live_urls_pass_gate_to_completed(
    db_session, _quiet_broadcast, mocker
):
    """A greenfield design doc that cites ONLY web URLs, all reachable, clears the
    web-only URL reachability gate and persists exactly one KnowledgeDocument with
    source_kind=='design' and the web URLs as source_citations."""
    mocker.patch(
        "app.knowledge_completion._probe_url_reachability",
        new=AsyncMock(return_value="reachable"),
    )
    project, task = await _make_design_task(db_session, project_name="DSGNgreenfield")

    document = {
        "title": "Design for the new payments API",
        "summary": "A greenfield payments API design grounded in web standards.",
        "body": "# Payments API\n\nThis design references the OpenAPI specification and REST best practices for a new payments service.",
        "citations": [
            "https://spec.openapis.org/oas/v3.1.0",
            "https://restfulapi.net/rest-api-design-tutorial/",
        ],
        "tags": ["design", "api"],
        "topic": "payments API design",
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    docs = await _docs_for(db_session, task.id)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_kind == "design"
    assert "https://spec.openapis.org/oas/v3.1.0" in doc.source_citations
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_greenfield_web_only_design_all_dead_urls_fail_gate(
    db_session, _quiet_broadcast, mocker
):
    """A greenfield design doc whose EVERY cited URL is definitively dead fails the
    web-only URL reachability gate: the task is marked FAILED with a reason
    containing 'unreachable', and no KnowledgeDocument is persisted."""
    mocker.patch(
        "app.knowledge_completion._probe_url_reachability",
        new=AsyncMock(return_value="dead"),
    )
    project, task = await _make_design_task(db_session, project_name="DSGNdeadurls")

    document = {
        "title": "Design for the new payments API",
        "summary": "A greenfield design referencing only dead links.",
        "body": "# Payments API\n\nThis design cites URLs that no longer resolve to real resources.",
        "citations": [
            "https://fabricated.example.invalid/api-spec",
            "https://also-fabricated.example.invalid/guide",
        ],
        "tags": ["design"],
        "topic": "payments API",
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    assert await _docs_for(db_session, task.id) == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    reason = task.context["evaluation_gate"]["reason"]
    assert "unreachable" in reason
    assert "not grounded in the codebase" not in reason
    assert "no usable source citation" not in reason


@pytest.mark.asyncio
async def test_greenfield_web_only_design_transient_errors_err_toward_acceptance(
    db_session, _quiet_broadcast, mocker
):
    """When ALL URL probes return 'transient' (network timeouts, 5xx errors), the
    gate errs toward acceptance — a flaky network must not block a valid greenfield
    design. The task is marked COMPLETED and exactly one document persists."""
    mocker.patch(
        "app.knowledge_completion._probe_url_reachability",
        new=AsyncMock(return_value="transient"),
    )
    project, task = await _make_design_task(db_session, project_name="DSGNtransient")

    document = {
        "title": "Design for the new payments API",
        "summary": "A greenfield design whose URL probes all return transient errors.",
        "body": "# Payments API\n\nThis design references the OpenAPI spec via web citations.",
        "citations": [
            "https://spec.openapis.org/oas/v3.1.0",
        ],
        "tags": ["design"],
        "topic": "payments API",
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    docs = await _docs_for(db_session, task.id)
    assert len(docs) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_greenfield_web_only_design_with_file_citation_bypasses_url_probe(
    db_session, _quiet_broadcast, mocker
):
    """When a design document has BOTH a web URL AND a file-path citation, the
    web-only URL reachability probe is skipped entirely — ``_design_web_only_grounding_miss``
    exits early when non-web citations are present. The task completes without calling
    ``_probe_url_reachability`` at all."""
    probe = mocker.patch(
        "app.knowledge_completion._probe_url_reachability",
        new=AsyncMock(return_value="dead"),
    )
    project, task = await _make_design_task(db_session, project_name="DSGNmixcite")

    document = {
        "title": "Design for the X subsystem",
        "summary": "Design referencing both real codebase files and external specs.",
        "body": "# X\n\nExtends existing modules as described in the referenced external specification.",
        "citations": [
            "backend/app/reaper.py",
            "https://spec.openapis.org/oas/v3.1.0",
        ],
        "tags": ["design"],
        "topic": "X",
        # Agent-stamped grounding: the file citation resolves to a real path.
        "_citation_grounding": {"file_citations": 1, "grounded": 1, "checked": True},
    }
    await _handle_design_completion(task, json.dumps(document), db_session)

    docs = await _docs_for(db_session, task.id)
    assert len(docs) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # Non-web citation short-circuits the web-only probe — never called.
    probe.assert_not_called()
