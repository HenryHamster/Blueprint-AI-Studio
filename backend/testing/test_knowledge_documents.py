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
from sqlmodel import select

from app.knowledge_completion import (
    _PENDING_DOCUMENT_KEY,
    _handle_design_completion,
    _handle_web_research_completion,
    _is_usable_citation,
    _parse_web_research_document,
)
from app.models import KnowledgeDocument, Task, TaskStatus
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
    )
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
        "origin_provenance",
        "sweep_id",
        "arm_label",
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
        "probes": {"grounding": {"grounded": 1}},
    }


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_groups_arms_by_sweep_id(db_session):
    """Compute docs sharing a sweep_id group into one comparative block, with
    criterion_met derived from the acceptance grade."""
    from app.knowledge_query import build_compute_sweep_groups

    project, task = await _make_web_research_task(db_session, project_name="SWG1")
    # Two arms in one sweep: one accepted (criterion met), one measured-only.
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="arm baseline",
        source_kind="compute_experiment",
        sweep_id="sw-1111",
        arm_label="baseline",
        forward_quality_verdict=None,  # not llm → measured_only → not met
    )
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="arm treatment",
        source_kind="compute_experiment",
        sweep_id="sw-1111",
        arm_label="treatment",
        forward_quality_verdict=_acceptance_verdict(),
        source_citations=["gs://bucket/result.json"],
    )

    groups = await build_compute_sweep_groups(db_session, project.id)
    # Returns {sweep_id: [_knowledge_index_entry, ...]}
    assert "sw-1111" in groups
    assert len(groups) == 1
    entries = groups["sw-1111"]
    assert len(entries) == 2
    by_label = {e["arm_label"]: e for e in entries}
    # baseline has no forward_quality_verdict → measured_only → not acceptance
    assert by_label["baseline"]["compute_acceptance_grade"] != "acceptance"
    # treatment has acceptance verdict → grade 'acceptance'
    assert by_label["treatment"]["compute_acceptance_grade"] == "acceptance"
    assert by_label["treatment"]["id"]
    assert by_label["treatment"]["title"] == "arm treatment"


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
    await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
        title="solo arm",
        source_kind="compute_experiment",
        sweep_id="sw-solo",
        arm_label="only-arm",
    )
    groups = await build_compute_sweep_groups(db_session, project.id)
    assert groups == {}


@pytest.mark.asyncio
async def test_build_compute_sweep_groups_newest_first_and_bounded(db_session):
    """Sweep groups are newest-first by latest arm and capped at the limit."""
    from datetime import datetime, timedelta

    from app.knowledge_query import build_compute_sweep_groups

    project, task = await _make_web_research_task(db_session, project_name="SWG3")
    base = datetime(2026, 1, 1, 12, 0, 0)
    # Three sweeps, each with two arms, at increasing timestamps.
    for i in range(3):
        for arm in ("a", "b"):
            await _make_knowledge_doc(
                db_session,
                project_id=project.id,
                source_task_id=task.id,
                title=f"sweep{i}-{arm}",
                source_kind="compute_experiment",
                sweep_id=f"sw-{i}",
                arm_label=arm,
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

    project_a, task_a = await _make_web_research_task(db_session, project_name="SWGA")
    project_b, task_b = await _make_web_research_task(db_session, project_name="SWGB")
    for arm in ("a", "b"):
        await _make_knowledge_doc(
            db_session,
            project_id=project_b.id,
            source_task_id=task_b.id,
            title=f"b-{arm}",
            source_kind="compute_experiment",
            sweep_id="sw-b",
            arm_label=arm,
        )
    assert await build_compute_sweep_groups(db_session, project_a.id) == {}


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

    project, task = await _make_web_research_task(db_session, project_name="CRIT")

    # ARM 1: criterion MET but grade is measured_only (deterministic path).
    # This is the bug-case: old code would have shown NOT MET because grade != acceptance.
    met_doc = await _make_knowledge_doc(
        db_session,
        project_id=project.id,
        source_task_id=task.id,
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
        source_task_id=task.id,
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
        source_task_id=task.id,
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

    # --- via build_compute_sweep_groups (same _knowledge_index_entry path) ---
    groups = await build_compute_sweep_groups(db_session, project.id)
    assert "sw-crit" in groups
    sweep_entries = groups["sw-crit"]
    by_label = {e["arm_label"]: e for e in sweep_entries}

    assert by_label["met_arm"]["criterion_met"] is True
    assert by_label["met_arm"]["compute_acceptance_grade"] != "acceptance"

    assert by_label["not_met_arm"]["criterion_met"] is False

    assert by_label["unmeasured_arm"]["criterion_met"] is None
