"""Strategic memory routed through KnowledgeDocument rows (design 2812f1ed).

The ``set_memory`` strategic op writes each behavioral fact as an
individually-addressable, trigram-searchable ``KnowledgeDocument`` row
(``source_kind='strategic_memory'``) instead of only appending to the capped
``Project.strategic_memory_notes`` JSONB list, and the strategist spawn payload
ships a compact memory index rather than the full JSONB list.

These live in a dedicated module (not test_directions.py) so the feature has a
focused, self-contained suite.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import KnowledgeDocument, Project, SourceKind, Task, TaskStatus


async def _make_strategic_project_and_task(db_session):
    """One project + one strategic_review task; returns (project, task_id)."""
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    project = Project(name="P-strategic-memory", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }
    return project, task_id


def _cleanup_strategic_overrides():
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(verify_agent_token, None)


async def _post_set_memory(data, task_id):
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as ac:
        return await ac.post(
            "/api/agent/strategic",
            json={"op": "set_memory", "task_id": task_id, "data": data},
        )


@pytest.mark.asyncio
async def test_set_memory_creates_strategic_memory_knowledge_document(db_session):
    """op=set_memory writes a strategic_memory KnowledgeDocument, retrievable by search."""
    from app.knowledge_query import list_knowledge_documents

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        resp = await _post_set_memory(
            {"note": "Always verify claims before escalating them."}, task_id
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "set_memory"
        assert body["source_kind"] == "strategic_memory"
        assert body["entry_count"] == 1
        doc_id = body["doc_id"]
        assert doc_id

        doc = (
            await db_session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.id == uuid.UUID(doc_id))
            )
        ).scalar_one()
        assert doc.source_kind == SourceKind.STRATEGIC_MEMORY.value
        assert doc.body == "Always verify claims before escalating them."
        assert doc.lifecycle_status == "consumed"
        assert doc.forward_quality_verdict == {
            "passed": True,
            "strategy": "agent_self_attested",
        }
        assert doc.source_citations == [{"task_id": str(uuid.UUID(task_id))}]

        # Retrievable via ./fetch knowledge --source-kind strategic_memory keyword search.
        listed = await list_knowledge_documents(
            db_session, project.id, source_kind="strategic_memory", q="verify claims"
        )
        assert listed["total"] == 1
        assert listed["documents"][0]["id"] == doc_id

        # Legacy JSONB dual-write preserved during the migration window.
        await db_session.refresh(project)
        assert len(project.strategic_memory_notes) == 1
        entry = project.strategic_memory_notes[0]
        assert entry["note"] == "Always verify claims before escalating them."
        assert entry["doc_id"] == doc_id
        assert "set_at" in entry
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_set_memory_rejects_empty_and_bad_topic(db_session):
    """set_memory validates note/body presence and the fixed topic taxonomy."""
    _project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        # missing note/body
        assert (await _post_set_memory({"other": "nope"}, task_id)).status_code == 400
        # empty string
        assert (await _post_set_memory({"note": ""}, task_id)).status_code == 400
        # whitespace-only
        assert (await _post_set_memory({"note": "   "}, task_id)).status_code == 400
        # non-dict data
        from app.main import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as ac:
            bad_data = await ac.post(
                "/api/agent/strategic",
                json={"op": "set_memory", "task_id": task_id, "data": "just a string"},
            )
        assert bad_data.status_code == 400
        # unknown topic rejected against the fixed taxonomy
        assert (
            await _post_set_memory({"note": "x", "topic": "not_a_real_topic"}, task_id)
        ).status_code == 400
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_set_memory_rich_fields_and_supersede(db_session):
    """set_memory stores title/summary/topic/tags and supersedes an old fact."""
    from app.knowledge_query import build_strategic_memory_index

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        first = await _post_set_memory(
            {
                "title": "Operator dislikes green-CI churn",
                "body": "The operator measures TRUE user-visible improvement, not green CI.",
                "summary": "Measure user-visible improvement, not CI churn.",
                "topic": "operator_preference",
                "tags": ["prioritization", "quality"],
            },
            task_id,
        )
        assert first.status_code == 200, first.text
        first_id = first.json()["doc_id"]

        # A superseding write dismisses the old fact in the same transaction.
        second = await _post_set_memory(
            {
                "body": "The operator measures shipped, verified improvements.",
                "topic": "operator_preference",
                "supersedes_id": first_id,
            },
            task_id,
        )
        assert second.status_code == 200, second.text
        assert second.json()["superseded_id"] == first_id

        old = (
            await db_session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.id == uuid.UUID(first_id))
            )
        ).scalar_one()
        assert old.lifecycle_status == "dismissed"
        assert old.dismissed_at is not None

        # An invalid supersedes_id is rejected.
        assert (
            await _post_set_memory(
                {"note": "x", "supersedes_id": "not-a-uuid"}, task_id
            )
        ).status_code == 400

        # The compact spawn index carries only the active (non-superseded) fact.
        index = await build_strategic_memory_index(db_session, project.id)
        ids = {e["id"] for e in index}
        assert first_id not in ids
        assert len(index) == 1
        assert index[0]["topic"] == "operator_preference"
        assert index[0]["title"]
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_set_memory_dual_write_caps_legacy_notes(db_session):
    """The legacy JSONB dual-write stays FIFO-capped at 100 entries."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        project.strategic_memory_notes = [
            {"note": f"entry {i}", "set_at": "2026-01-01T00:00:00"} for i in range(100)
        ]
        flag_modified(project, "strategic_memory_notes")
        db_session.add(project)
        await db_session.commit()

        resp = await _post_set_memory({"note": "newest note"}, task_id)
        assert resp.status_code == 200, resp.text

        await db_session.refresh(project)
        assert len(project.strategic_memory_notes) == 100
        assert project.strategic_memory_notes[0]["note"] == "entry 1"
        assert project.strategic_memory_notes[-1]["note"] == "newest note"
    finally:
        _cleanup_strategic_overrides()
