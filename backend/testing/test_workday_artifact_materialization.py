import base64
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlmodel import select

from app.auth import create_agent_token
from app.models import (
    ArtifactRef,
    MessageHandlingState,
    MessageType,
    Project,
    ProjectMessage,
    Task,
)
from app.routers.agent_strategic_messages import (
    PermanentWorkdayArtifactError,
    _materialize_workday_artifact_attachments,
)
from app.storage import (
    StorageUriError,
    artifact_ref_render_uri,
)


async def _project(db_session):
    project = Project(name="Artifact poll", user_id="artifact-poll-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _task(db_session, project):
    task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="artifact-poll-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _message(db_session, project, context):
    message = ProjectMessage(
        project_id=project.id,
        from_type="user",
        status="open",
        message_type=MessageType.QUESTION,
        content="Review the exact revision.",
        context=context,
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    return message


async def _resident_task(
    db_session,
    project,
    *,
    generation: int = 1,
    session_id: str = "artifact-resident-session",
):
    task = await _task(db_session, project)
    task.context = {
        "_resident_session": True,
        "strategic_session_id": session_id,
        "_resident_worker_generation": generation,
    }
    project.vision = {
        "_resident_strategic_session": {
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": generation,
            "status": "active",
        }
    }
    db_session.add_all([project, task])
    await db_session.commit()
    return task


def _headers(task):
    return {
        "x-agent-token": create_agent_token(
            str(task.id), "artifact-poll-vm"
        )
    }


async def _whiteboard_ref(
    db_session,
    project,
    *,
    revision: int,
    generation: int | None = 1,
    size_bytes: int = 64,
):
    tool_id = uuid.uuid4()
    attachment_id = uuid.uuid4()
    storage_uri = artifact_ref_render_uri(
        project.id, tool_id, revision, attachment_id
    )
    retrieval = {
        "capability": "project_tool_rendered_image",
        "storage_uri": storage_uri,
        "attachment_id": str(attachment_id),
        "mime_type": "image/png",
        "image_checksum": "2" * 64,
        "size_bytes": size_bytes,
    }
    if generation is not None:
        retrieval["generation"] = generation
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=tool_id,
        kind="whiteboard",
        revision=revision,
        checksum="3" * 64,
        author_user_id="operator",
        content_snapshot={"node_count": 1},
        retrieval_metadata=retrieval,
    )
    db_session.add(ref)
    await db_session.commit()
    await db_session.refresh(ref)
    return ref, storage_uri


@pytest.mark.asyncio
async def test_materializes_exact_notes_snapshot(db_session):
    project = await _project(db_session)
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=7,
        checksum="a" * 64,
        author_user_id="operator",
        content_snapshot={
            "text": "# Exact revision\n\nUse this text.",
            "truncated": False,
            "original_char_count": 32,
        },
        retrieval_metadata={
            "capability": "inline_bounded_text",
            "content_truncated": False,
        },
    )
    db_session.add(ref)
    await db_session.commit()
    message = await _message(
        db_session,
        project,
        {
            "attachments": [{"type": "file", "name": "ordinary.txt"}],
            "artifact_refs": [{"artifact_ref_id": str(ref.id)}],
        },
    )

    result = await _materialize_workday_artifact_attachments(
        db_session, project.id, [message]
    )

    [attachment] = result[message.id]
    assert attachment["type"] == "file"
    assert attachment["mime_type"] == "text/markdown"
    assert attachment["artifact_ref_id"] == str(ref.id)
    assert base64.b64decode(attachment["data"]).decode() == (
        "# Exact revision\n\nUse this text."
    )
    assert attachment["truncated"] is False
    assert attachment["snapshot_char_count"] == 32
    assert attachment["original_char_count"] == 32
    assert message.context["attachments"] == [
        {"type": "file", "name": "ordinary.txt"}
    ]


@pytest.mark.asyncio
async def test_notes_snapshot_surfaces_bounded_truncation_to_the_model(db_session):
    project = await _project(db_session)
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=8,
        checksum="c" * 64,
        author_user_id="operator",
        content_snapshot={
            "text": "first five",
            "truncated": True,
            "original_char_count": 50,
        },
        retrieval_metadata={
            "capability": "inline_bounded_text",
            "content_truncated": True,
        },
    )
    db_session.add(ref)
    await db_session.commit()
    message = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )

    result = await _materialize_workday_artifact_attachments(
        db_session, project.id, [message]
    )

    [attachment] = result[message.id]
    delivered = base64.b64decode(attachment["data"]).decode()
    assert delivered.startswith("first five")
    assert (
        "[Artifact snapshot truncated: showing the first 10 of 50 characters.]"
        in delivered
    )
    assert attachment["truncated"] is True
    assert attachment["snapshot_char_count"] == 10
    assert attachment["original_char_count"] == 50


@pytest.mark.asyncio
async def test_materializes_whiteboard_as_short_lived_signed_image(
    db_session, monkeypatch
):
    project = await _project(db_session)
    tool_id = uuid.uuid4()
    attachment_id = uuid.uuid4()
    storage_uri = artifact_ref_render_uri(
        project.id, tool_id, 4, attachment_id
    )
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=tool_id,
        kind="whiteboard",
        revision=4,
        checksum="b" * 64,
        author_user_id="operator",
        content_snapshot={"node_count": 3},
        retrieval_metadata={
            "capability": "project_tool_rendered_image",
            "storage_uri": storage_uri,
            "attachment_id": str(attachment_id),
            "mime_type": "image/png",
            "image_checksum": "d" * 64,
            "size_bytes": 128,
            "generation": 7,
        },
    )
    db_session.add(ref)
    await db_session.commit()
    message = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )
    signed = "https://storage.googleapis.com/bucket/board.png?X-Goog-Signature=sig"
    signer = AsyncMock(return_value=signed)
    matcher = AsyncMock(return_value=True)
    monkeypatch.setattr("app.storage.generate_download_signed_url", signer)
    monkeypatch.setattr("app.storage.artifact_object_matches_receipt", matcher)

    result = await _materialize_workday_artifact_attachments(
        db_session, project.id, [message]
    )

    assert result[message.id] == [
        {
            "type": "image",
            "mime_type": "image/png",
            "name": "Whiteboard revision 4",
            "url": signed,
            "artifact_ref_id": str(ref.id),
            "required": True,
            "sha256": "d" * 64,
            "checksum": "b" * 64,
            "image_checksum": "d" * 64,
        }
    ]
    matcher.assert_awaited_once_with(
        storage_uri,
        expected_size_bytes=128,
        expected_generation=7,
    )
    signer.assert_awaited_once_with(storage_uri, expiration_minutes=10)


@pytest.mark.asyncio
async def test_fails_closed_when_promised_revision_is_missing(db_session):
    project = await _project(db_session)
    message = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(uuid.uuid4())}]},
    )

    with pytest.raises(ValueError, match="promised artifact revisions"):
        await _materialize_workday_artifact_attachments(
            db_session, project.id, [message]
        )


@pytest.mark.asyncio
async def test_rejects_non_hex_revision_checksum(db_session):
    project = await _project(db_session)
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=1,
        checksum="z" * 64,
        author_user_id="operator",
        content_snapshot={
            "text": "text",
            "truncated": False,
            "original_char_count": 4,
        },
        retrieval_metadata={
            "capability": "inline_bounded_text",
            "content_truncated": False,
        },
    )
    db_session.add(ref)
    await db_session.commit()
    message = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        await _materialize_workday_artifact_attachments(
            db_session, project.id, [message]
        )


@pytest.mark.asyncio
async def test_poll_quarantines_missing_grant_and_delivers_valid_neighbors_once(
    client: AsyncClient, db_session
):
    project = await _project(db_session)
    task = await _task(db_session, project)
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=2,
        checksum="e" * 64,
        author_user_id="operator",
        content_snapshot={
            "text": "available",
            "truncated": False,
            "original_char_count": 9,
        },
        retrieval_metadata={
            "capability": "inline_bounded_text",
            "content_truncated": False,
        },
    )
    db_session.add(ref)
    later_ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=3,
        checksum="1" * 64,
        author_user_id="operator",
        content_snapshot={
            "text": "later",
            "truncated": False,
            "original_char_count": 5,
        },
        retrieval_metadata={
            "capability": "inline_bounded_text",
            "content_truncated": False,
        },
    )
    db_session.add(later_ref)
    await db_session.commit()
    first = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )
    poison = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(uuid.uuid4())}]},
    )
    later = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(later_ref.id)}]},
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    await db_session.refresh(poison)
    assert poison.status == "resolved"
    assert poison.handling_state == MessageHandlingState.SUPERSEDED
    assert poison.context["artifact_handoff_failure"]["workday_inspected"] is False
    assert "did not receive or inspect" in poison.resolution

    notices = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key
                    == f"artifact-handoff-failed:{poison.id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(notices) == 1
    assert notices[0].reply_to_message_id == poison.id
    assert notices[0].sequence > 0
    assert notices[0].surface == "conversation"
    assert notices[0].context["audience"] == "operator-action-needed"
    assert notices[0].context["workday_inspected"] is False
    assert "did not receive or inspect" in notices[0].content

    retry = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert retry.status_code == 200
    assert [item["id"] for item in retry.json()["messages"]] == [
        str(first.id),
        str(later.id),
    ]
    duplicate_notices = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.idempotency_key
                    == f"artifact-handoff-failed:{poison.id}"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(duplicate_notices) == 1


@pytest.mark.asyncio
async def test_poll_retries_when_signed_whiteboard_url_is_not_agent_compatible(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project(db_session)
    task = await _task(db_session, project)
    tool_id = uuid.uuid4()
    attachment_id = uuid.uuid4()
    storage_uri = artifact_ref_render_uri(
        project.id, tool_id, 3, attachment_id
    )
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=tool_id,
        kind="whiteboard",
        revision=3,
        checksum="f" * 64,
        author_user_id="operator",
        content_snapshot={"node_count": 1},
        retrieval_metadata={
            "capability": "project_tool_rendered_image",
            "storage_uri": storage_uri,
            "attachment_id": str(attachment_id),
            "mime_type": "image/png",
            "image_checksum": "0" * 64,
            "size_bytes": 64,
            "generation": 3,
        },
    )
    db_session.add(ref)
    await db_session.commit()
    source = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )
    monkeypatch.setattr(
        "app.storage.artifact_object_matches_receipt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.storage.generate_download_signed_url",
        AsyncMock(
            return_value=(
                "https://example.com/board.png?X-Goog-Signature=sig"
            )
        ),
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert "messages" not in response.json()
    await db_session.refresh(source)
    assert source.status == "open"
    assert source.handling_state == MessageHandlingState.PENDING
    notices = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key
                    == f"artifact-handoff-failed:{source.id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert notices == []

@pytest.mark.asyncio
async def test_legacy_whiteboard_without_generation_remains_deliverable(
    db_session, monkeypatch
):
    project = await _project(db_session)
    ref, storage_uri = await _whiteboard_ref(
        db_session, project, revision=10, generation=None
    )
    message = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )
    matcher = AsyncMock(return_value=True)
    signer = AsyncMock(
        return_value=(
            "https://storage.googleapis.com/bucket/legacy.png"
            "?X-Goog-Signature=sig"
        )
    )
    monkeypatch.setattr("app.storage.artifact_object_matches_receipt", matcher)
    monkeypatch.setattr("app.storage.generate_download_signed_url", signer)

    result = await _materialize_workday_artifact_attachments(
        db_session, project.id, [message]
    )

    assert result[message.id][0]["artifact_ref_id"] == str(ref.id)
    assert result[message.id][0]["required"] is True
    matcher.assert_awaited_once_with(
        storage_uri,
        expected_size_bytes=64,
        expected_generation=None,
    )


@pytest.mark.asyncio
async def test_missing_whiteboard_object_is_permanent_and_never_signed(
    db_session, monkeypatch
):
    project = await _project(db_session)
    ref, _ = await _whiteboard_ref(db_session, project, revision=11)
    message = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )
    signer = AsyncMock()
    monkeypatch.setattr(
        "app.storage.artifact_object_matches_receipt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("app.storage.generate_download_signed_url", signer)

    with pytest.raises(
        PermanentWorkdayArtifactError,
        match="render is missing",
    ):
        await _materialize_workday_artifact_attachments(
            db_session, project.id, [message]
        )
    signer.assert_not_awaited()


@pytest.mark.asyncio
async def test_repaired_source_aborts_page_without_superseding_or_cursor_skip(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project(db_session)
    task = await _task(db_session, project)
    source = await _message(db_session, project, {})
    materialize = AsyncMock(
        side_effect=[
            PermanentWorkdayArtifactError(source.id, "stale failure"),
            {},
        ]
    )
    monkeypatch.setattr(
        "app.routers.agent_strategic_messages."
        "_materialize_workday_artifact_attachments",
        materialize,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    await db_session.refresh(source)
    assert source.status == "open"
    assert source.handling_state == MessageHandlingState.PENDING
    notice = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.idempotency_key
                == f"artifact-handoff-failed:{source.id}"
            )
        )
    ).scalar_one_or_none()
    assert notice is None


@pytest.mark.asyncio
async def test_resident_authority_replacement_during_materialization_is_rejected(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project(db_session)
    task = await _resident_task(db_session, project)
    source = await _message(db_session, project, {})

    async def replace_authority(session, project_id, _messages, **_kwargs):
        replacement = Task(
            project_id=project_id,
            payloads=[{}],
            vm_instance_id="artifact-poll-replacement-vm",
            context={
                "_resident_session": True,
                "strategic_session_id": "artifact-resident-session",
                "_resident_worker_generation": 2,
            },
        )
        session.add(replacement)
        await session.flush()
        replacement_vision = {
            "_resident_strategic_session": {
                "session_id": "artifact-resident-session",
                "task_id": str(replacement.id),
                "worker_generation": 2,
                "status": "active",
            }
        }
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(vision=replacement_vision)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return {}

    monkeypatch.setattr(
        "app.routers.agent_strategic_messages."
        "_materialize_workday_artifact_attachments",
        replace_authority,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )

    assert response.status_code == 409
    assert "superseded or inactive" in response.text
    await db_session.refresh(source)
    assert source.status == "open"
    assert source.handling_state == MessageHandlingState.PENDING


@pytest.mark.asyncio
async def test_resident_page_mutation_during_materialization_trips_final_fence(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project(db_session)
    task = await _resident_task(db_session, project)
    source = await _message(db_session, project, {})

    async def mutate_page(session, _project_id, _messages, **_kwargs):
        await session.execute(
            update(ProjectMessage)
            .where(ProjectMessage.id == source.id)
            .values(content="Changed while remote artifact work was in flight.")
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return {}

    monkeypatch.setattr(
        "app.routers.agent_strategic_messages."
        "_materialize_workday_artifact_attachments",
        mutate_page,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert "Conversation changed during artifact delivery" in response.text
    await db_session.refresh(source)
    assert source.status == "open"
    assert source.content == "Changed while remote artifact work was in flight."


@pytest.mark.asyncio
async def test_transient_whiteboard_metadata_failure_preserves_source(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project(db_session)
    task = await _task(db_session, project)
    ref, _ = await _whiteboard_ref(db_session, project, revision=12)
    source = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )
    signer = AsyncMock()
    monkeypatch.setattr(
        "app.storage.artifact_object_matches_receipt",
        AsyncMock(side_effect=RuntimeError("GCS metadata timeout")),
    )
    monkeypatch.setattr("app.storage.generate_download_signed_url", signer)

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    signer.assert_not_awaited()
    await db_session.refresh(source)
    assert source.status == "open"
    assert source.handling_state == MessageHandlingState.PENDING
    notice = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.idempotency_key
                == f"artifact-handoff-failed:{source.id}"
            )
        )
    ).scalar_one_or_none()
    assert notice is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch_reason",
    [
        "Artifact object size does not match receipt",
        "Artifact object generation does not match receipt",
    ],
)
async def test_whiteboard_receipt_mismatch_is_permanently_quarantined(
    client: AsyncClient,
    db_session,
    monkeypatch,
    mismatch_reason,
):
    project = await _project(db_session)
    task = await _task(db_session, project)
    ref, _ = await _whiteboard_ref(db_session, project, revision=13)
    source = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(ref.id)}]},
    )
    signer = AsyncMock()
    monkeypatch.setattr(
        "app.storage.artifact_object_matches_receipt",
        AsyncMock(side_effect=StorageUriError(mismatch_reason)),
    )
    monkeypatch.setattr("app.storage.generate_download_signed_url", signer)

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    signer.assert_not_awaited()
    await db_session.refresh(source)
    assert source.status == "resolved"
    assert source.handling_state == MessageHandlingState.SUPERSEDED
    assert source.context["artifact_handoff_failure"]["reason"] == mismatch_reason
    notice = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.idempotency_key
                == f"artifact-handoff-failed:{source.id}"
            )
        )
    ).scalar_one()
    assert notice.reply_to_message_id == source.id
    assert notice.context["reason"] == mismatch_reason


@pytest.mark.asyncio
async def test_required_images_progress_oldest_first_across_poll_batches(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project(db_session)
    task = await _task(db_session, project)
    refs = [
        (await _whiteboard_ref(db_session, project, revision=revision))[0]
        for revision in range(1, 11)
    ]
    first = await _message(
        db_session,
        project,
        {
            "artifact_refs": [
                {"artifact_ref_id": str(ref.id)} for ref in refs[:5]
            ]
        },
    )
    second = await _message(
        db_session,
        project,
        {
            "artifact_refs": [
                {"artifact_ref_id": str(ref.id)} for ref in refs[5:]
            ]
        },
    )
    monkeypatch.setattr(
        "app.storage.artifact_object_matches_receipt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.storage.generate_download_signed_url",
        AsyncMock(
            return_value=(
                "https://storage.googleapis.com/bucket/board.png"
                "?X-Goog-Signature=sig"
            )
        ),
    )

    first_poll = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert first_poll.status_code == 200
    assert [item["id"] for item in first_poll.json()["messages"]] == [
        str(first.id)
    ]

    second_poll = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"since": first.created_at.isoformat()},
        headers=_headers(task),
    )
    assert second_poll.status_code == 200
    assert [item["id"] for item in second_poll.json()["messages"]] == [
        str(first.id),
        str(second.id),
    ]


@pytest.mark.asyncio
async def test_single_message_exceeding_image_budget_is_quarantined_not_starved(
    client: AsyncClient, db_session, monkeypatch
):
    """A message with >8 whiteboard refs raises PermanentWorkdayArtifactError
    (not WorkdayArtifactBatchLimit), gets quarantined, and does not block
    delivery of neighboring messages."""
    project = await _project(db_session)
    task = await _task(db_session, project)

    # 9 whiteboard refs on a single message — exceeds _WORKDAY_MAX_REQUIRED_IMAGES=8
    refs = [
        (await _whiteboard_ref(db_session, project, revision=r))[0]
        for r in range(1, 10)
    ]
    before = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(refs[0].id)}]},
    )
    overloaded = await _message(
        db_session,
        project,
        {
            "artifact_refs": [
                {"artifact_ref_id": str(ref.id)} for ref in refs
            ]
        },
    )
    after = await _message(
        db_session,
        project,
        {"artifact_refs": [{"artifact_ref_id": str(refs[0].id)}]},
    )

    monkeypatch.setattr(
        "app.storage.artifact_object_matches_receipt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.storage.generate_download_signed_url",
        AsyncMock(
            return_value=(
                "https://storage.googleapis.com/bucket/board.png"
                "?X-Goog-Signature=sig"
            )
        ),
    )

    first_poll = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    # Overloaded message is permanently quarantined; poll retries so agent is not starved
    assert first_poll.status_code == 503
    assert first_poll.headers["Retry-After"] == "1"

    await db_session.refresh(overloaded)
    assert overloaded.status == "resolved"
    assert overloaded.handling_state == MessageHandlingState.SUPERSEDED
    assert (
        "required image attachment limit exceeded"
        in overloaded.context["artifact_handoff_failure"]["reason"]
    )

    notice = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.idempotency_key
                == f"artifact-handoff-failed:{overloaded.id}"
            )
        )
    ).scalar_one()
    assert notice.reply_to_message_id == overloaded.id
    assert notice.context["audience"] == "operator-action-needed"

    # On retry, before and after messages are delivered without the overloaded one
    retry = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert retry.status_code == 200
    delivered_ids = [item["id"] for item in retry.json()["messages"]]
    assert str(overloaded.id) not in delivered_ids
    assert str(before.id) in delivered_ids
    assert str(after.id) in delivered_ids
