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


async def _make_web_research_task(db_session, *, project_name="WR"):
    project = make_project(name=project_name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Web Research: X",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "web_research", "topic": "X", "base_branch": "main"}
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
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
    assert task.plan_output["knowledge_document"]["title"] == document["title"]
    assert "body" not in task.plan_output["knowledge_document"]


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
async def test_grader_enabled_escalates_and_parks(
    db_session, _quiet_broadcast, mocker
):
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

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_grader_fail_marks_failed_and_persists_no_document(
    db_session, _quiet_broadcast
):
    """RESUME pass with a FAIL verdict marks the source FAILED and persists no
    document — a failing independent grade never completes clean."""
    project, task = await _make_web_research_task(db_session, project_name="WRfailgrade")

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

    resp = await client.get(
        f"/api/admin/projects/{proj_a.id}/knowledge-documents"
    )
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
