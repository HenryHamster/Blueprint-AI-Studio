"""Tests for exact-revision human-tool handoff to the Strategic Resident."""

from __future__ import annotations

import asyncio
import hashlib
import io
from contextlib import nullcontext
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlmodel import select

from app.artifact_refs import (
    RESIDENT_ARTIFACT_REF_GRANTS_KEY,
    artifact_ref_projection,
    create_tool_artifact_ref,
)
from app.artifact_ref_cleanup import (
    _claim_intent,
    _finish_intent,
    drain_artifact_ref_cleanup_intents,
)
from app.artifact_ref_promotion import (
    drain_artifact_ref_promotion_intents,
    prepare_artifact_ref_promotion_intent,
    record_artifact_ref_promotion_effect,
)
from app.database import AsyncSessionLocal
from app.models import (
    Artifact,
    ArtifactRef,
    ArtifactRefCleanupIntent,
    ArtifactRefPromotionIntent,
    ArtifactType,
    Project,
    ProjectMember,
    ProjectMessageAttachmentUpload,
    ProjectTool,
    Task,
    _utcnow,
)
from app.routers.admin_project_strategic_session import (
    StrategicSessionMessageCreate,
    _validated_message_scoped_context,
)
from app.routers.agent_resident_artifacts import (
    MAX_ARTIFACT_RETRIEVALS_PER_TURN,
    _register_artifact_retrieval,
    get_resident_turn_artifact,
)
from app.resident_agent_rate_limits import (
    resident_remote_rate_key,
    resident_task_token_rate_key,
)
from app.strategic_resident_context import _project_revisioned_refs
from testing.conftest import USE_POSTGRES
from app.storage import (
    ARTIFACT_BUCKET_NAME,
    ATTACHMENT_DELETE_RETRY_DEADLINE_SECONDS,
    ATTACHMENT_DELETE_RPC_TIMEOUT_SECONDS,
    ArtifactRefRenderPromotion,
    StorageUriError,
    artifact_ref_render_uri,
    delete_storage_uri_best_effort,
    delete_artifact_ref_project_prefix_best_effort,
    promote_artifact_ref_render,
)
from app.strategic_resident_checkpointing import initialize_strategic_resident


TEST_USER_ID = "test-user-123"


async def _project(session, *, user_id: str = TEST_USER_ID) -> Project:
    project = Project(name="ArtifactRef test", user_id=user_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=user_id, role="owner"))
    await session.commit()
    return project


@pytest.mark.asyncio
async def test_notes_artifact_ref_endpoint_freezes_exact_bounded_revision(
    client: AsyncClient, db_session
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="notes",
        slug="notes",
        title="Notes",
        content={"text": "x" * 20_000},
        content_revision=7,
        created_by=TEST_USER_ID,
    )
    db_session.add(tool)
    await db_session.commit()
    await db_session.refresh(tool)

    response = await client.post(
        f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
        json={"expected_revision": 7},
    )
    assert response.status_code == 201, response.text
    chip = response.json()["artifact_ref"]
    assert chip["kind"] == "notes"
    assert chip["revision"] == 7
    assert len(chip["checksum"]) == 64
    assert chip["artifact_ref_id"] == chip["id"]
    assert len(chip["excerpt"]) == 2_000
    assert chip["retrieval"] == {
        "capability": "inline_bounded_text",
        "href": f"/api/agent/resident-turn/artifact?artifact_ref_id={chip['id']}",
    }

    fetched = await client.get(
        f"/api/admin/projects/{project.id}/artifact-refs/{chip['id']}"
    )
    assert fetched.status_code == 200
    assert len(fetched.json()["content"]["text"]) == 16_000
    assert fetched.json()["content"]["truncated"] is True

    deleted = await client.delete(
        f"/api/admin/projects/{project.id}/tools/{tool.id}"
    )
    assert deleted.status_code == 200
    after_delete = await client.get(
        f"/api/admin/projects/{project.id}/artifact-refs/{chip['id']}"
    )
    assert after_delete.status_code == 200
    assert after_delete.json()["artifact_ref"]["tool_id"] == str(tool.id)


@pytest.mark.asyncio
async def test_artifact_ref_endpoint_reports_rebaseable_revision_conflict(
    client: AsyncClient, db_session
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="notes",
        slug="notes",
        title="Notes",
        content={"text": "new"},
        content_revision=4,
    )
    db_session.add(tool)
    await db_session.commit()
    await db_session.refresh(tool)

    response = await client.post(
        f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
        json={"expected_revision": 3},
    )
    assert response.status_code == 409
    detail = response.json()["error"]["message"]
    assert detail == {
        "code": "tool_revision_conflict",
        "expected_revision": 3,
        "current_revision": 4,
        "action": "reload_or_rebase",
    }


@pytest.mark.asyncio
async def test_whiteboard_artifact_consumes_render_upload_once(db_session, mocker):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="whiteboard",
        slug="whiteboard",
        title="Board",
        content={"strokes": [{"x": 1}], "nodes": []},
        content_revision=2,
    )
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=f"gs://bucket/message-attachments/{project.id}/board.png",
        filename="board.png",
        content_type="image/svg+xml",
        expected_size_bytes=42,
        expires_at=_utcnow() + timedelta(minutes=10),
    )
    db_session.add(tool)
    db_session.add(receipt)
    await db_session.commit()
    await db_session.refresh(tool)
    await db_session.refresh(receipt)

    rendered_image = {
        "attachment_id": str(receipt.attachment_id),
        "storage_uri": receipt.storage_uri,
        "checksum": hashlib.sha256(b"image").hexdigest(),
        "width": 800,
        "height": 600,
        "_durable_storage_uri": artifact_ref_render_uri(
            project.id, tool.id, 2, receipt.attachment_id
        ),
    }
    with pytest.raises(ValueError, match="PNG, JPEG, or WebP"):
        await create_tool_artifact_ref(
            db_session,
            project_id=project.id,
            tool_id=tool.id,
            actor_user_id=TEST_USER_ID,
            expected_revision=2,
            rendered_image=rendered_image,
        )
    receipt.content_type = "image/png"
    db_session.add(receipt)
    claim_id = uuid.uuid4()
    promotion_intent = ArtifactRefPromotionIntent(
        project_id=project.id,
        tool_id=tool.id,
        revision=2,
        receipt_id=receipt.attachment_id,
        actor_user_id=TEST_USER_ID,
        source_storage_uri=receipt.storage_uri,
        candidate_storage_uri=rendered_image["_durable_storage_uri"],
        expected_size_bytes=receipt.expected_size_bytes,
        expected_sha256=rendered_image["checksum"],
        render_width=800,
        render_height=600,
        state="promoted",
        claim_id=claim_id,
        lease_expires_at=_utcnow() + timedelta(minutes=20),
        effect_deadline_at=_utcnow() + timedelta(minutes=5),
        settle_until=_utcnow() + timedelta(minutes=15),
        destination_generation=17,
    )
    db_session.add(promotion_intent)
    await db_session.commit()
    frozen_clock = mocker.patch(
        "app.artifact_refs._utcnow",
        return_value=receipt.expires_at + timedelta(seconds=1),
    )
    artifact_ref = await create_tool_artifact_ref(
        db_session,
        project_id=project.id,
        tool_id=tool.id,
        actor_user_id=TEST_USER_ID,
        expected_revision=2,
        rendered_image=rendered_image,
        promotion_intent_id=promotion_intent.id,
        promotion_claim_id=claim_id,
    )
    await db_session.commit()
    await db_session.refresh(receipt)
    mocker.stop(frozen_clock)
    assert receipt.artifact_ref_id == artifact_ref.id
    assert receipt.consumed_at is not None
    assert artifact_ref.content_snapshot["stroke_count"] == 1
    assert artifact_ref.retrieval_metadata["capability"] == "project_tool_rendered_image"

    replay_receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=f"gs://bucket/message-attachments/{project.id}/replay.png",
        filename="replay.png",
        content_type="image/png",
        expected_size_bytes=43,
        expires_at=_utcnow() + timedelta(minutes=10),
    )
    db_session.add(replay_receipt)
    await db_session.commit()
    await db_session.refresh(replay_receipt)
    adopted = await create_tool_artifact_ref(
        db_session,
        project_id=project.id,
        tool_id=tool.id,
        actor_user_id=TEST_USER_ID,
        expected_revision=2,
        rendered_image={
            **rendered_image,
            "attachment_id": str(replay_receipt.attachment_id),
            "storage_uri": replay_receipt.storage_uri,
            "_durable_storage_uri": artifact_ref_render_uri(
                project.id, tool.id, 2, replay_receipt.attachment_id
            ),
        },
    )
    await db_session.commit()
    await db_session.refresh(replay_receipt)
    assert adopted.id == artifact_ref.id
    assert replay_receipt.consumed_at is None
    assert replay_receipt.artifact_ref_id is None
    assert replay_receipt.expires_at <= _utcnow()


@pytest.mark.asyncio
async def test_whiteboard_endpoint_promotes_before_lock_and_stores_only_durable_uri(
    client: AsyncClient, db_session, mocker
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="whiteboard",
        slug="whiteboard",
        title="Board",
        content={"strokes": [], "nodes": []},
        content_revision=3,
    )
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=(
            f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
            f"{project.id}/{uuid.uuid4()}/board.png"
        ),
        filename="board.png",
        content_type="image/png",
        expected_size_bytes=5,
        expires_at=_utcnow() + timedelta(minutes=10),
    )
    # The receipt identity is part of the authorized source prefix.
    receipt.storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{receipt.attachment_id}/board.png"
    )
    db_session.add_all([tool, receipt])
    await db_session.commit()
    await db_session.refresh(tool)
    await db_session.refresh(receipt)
    durable_uri = artifact_ref_render_uri(
        project.id, tool.id, 3, receipt.attachment_id
    )

    promotion_calls = 0

    async def _promote(*args, **kwargs):
        nonlocal promotion_calls
        promotion_calls += 1
        return ArtifactRefRenderPromotion(storage_uri=durable_uri, created=True, generation=21)

    promote = mocker.patch(
        "app.artifact_ref_promotion.promote_artifact_ref_render",
        new=_promote,
    )
    response = await client.post(
        f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
        json={
            "expected_revision": 3,
            "rendered_image": {
                "attachment_id": str(receipt.attachment_id),
                "storage_uri": receipt.storage_uri,
                "checksum": hashlib.sha256(b"image").hexdigest(),
                "width": 800,
                "height": 600,
            },
        },
    )
    assert response.status_code == 201, response.text
    ref_id = response.json()["artifact_ref"]["id"]
    fetched = await client.get(
        f"/api/admin/projects/{project.id}/artifact-refs/{ref_id}"
    )
    assert fetched.json()["retrieval"]["storage_uri"] == durable_uri
    assert fetched.json()["retrieval"]["storage_uri"] != receipt.storage_uri
    assert promote is not None

    retry = await client.post(
        f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
        json={
            "expected_revision": 3,
            "rendered_image": {
                "attachment_id": str(receipt.attachment_id),
                "storage_uri": receipt.storage_uri,
                "checksum": hashlib.sha256(b"image").hexdigest(),
            },
        },
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["artifact_ref"]["id"] == ref_id
    assert promotion_calls == 1


@pytest.mark.asyncio
async def test_202_keeps_tracked_promotion_alive_until_prompt_ownership(
    client: AsyncClient, db_session, mocker
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="whiteboard",
        slug="tracked-board",
        title="Tracked board",
        content={"strokes": []},
        content_revision=1,
    )
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri="placeholder",
        filename="board.png",
        content_type="image/png",
        expected_size_bytes=5,
        expires_at=_utcnow() + timedelta(minutes=10),
    )
    receipt.storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{receipt.attachment_id}/board.png"
    )
    db_session.add_all([tool, receipt])
    await db_session.commit()
    await db_session.refresh(tool)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _promote(*args, **kwargs):
        started.set()
        await release.wait()
        return ArtifactRefRenderPromotion(
            storage_uri=artifact_ref_render_uri(
                project.id, tool.id, 1, receipt.attachment_id
            ),
            created=True,
            generation=41,
        )

    async def _return_202_without_cancelling(task):
        await started.wait()
        assert not task.cancelled()
        raise TimeoutError

    mocker.patch(
        "app.artifact_ref_promotion.promote_artifact_ref_render",
        new=_promote,
    )
    mocker.patch(
        "app.routers.admin_project_tools._await_tracked_artifact_ref_promotion",
        new=_return_202_without_cancelling,
    )
    response = await client.post(
        f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
        json={
            "expected_revision": 1,
            "rendered_image": {
                "attachment_id": str(receipt.attachment_id),
                "storage_uri": receipt.storage_uri,
                "checksum": hashlib.sha256(b"image").hexdigest(),
            },
        },
    )
    assert response.status_code == 202, response.text
    intent_id = uuid.UUID(response.json()["promotion_intent_id"])
    release.set()

    owned = None
    for _ in range(50):
        await asyncio.sleep(0.05)
        async with AsyncSessionLocal() as observer:
            intent = await observer.get(ArtifactRefPromotionIntent, intent_id)
            if intent is not None and intent.state == "owned":
                owned = intent
                break
    assert owned is not None
    assert owned.destination_generation == 41
    assert owned.artifact_ref_id is not None


@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="membership revocation concurrency requires PostgreSQL row locks",
)
async def test_membership_revoked_during_promotion_fails_final_fence_and_cleans(
    client: AsyncClient, db_session, mocker
):
    from sqlalchemy import delete

    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="whiteboard",
        slug="whiteboard",
        title="Board",
        content={"strokes": [], "nodes": []},
        content_revision=1,
    )
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri="placeholder",
        filename="board.png",
        content_type="image/png",
        expected_size_bytes=5,
        expires_at=_utcnow() + timedelta(minutes=10),
    )
    receipt.storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{receipt.attachment_id}/board.png"
    )
    db_session.add_all([tool, receipt])
    await db_session.commit()
    durable_uri = artifact_ref_render_uri(
        project.id, tool.id, 1, receipt.attachment_id
    )

    async def _promote(*args, **kwargs):
        async with AsyncSessionLocal() as revoke_session:
            await revoke_session.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == TEST_USER_ID,
                )
            )
            await revoke_session.commit()
        return ArtifactRefRenderPromotion(
            storage_uri=durable_uri,
            created=True,
            generation=21,
        )

    mocker.patch(
        "app.artifact_ref_promotion.promote_artifact_ref_render",
        new=_promote,
    )
    response = await client.post(
        f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
        json={
            "expected_revision": 1,
            "rendered_image": {
                "attachment_id": str(receipt.attachment_id),
                "storage_uri": receipt.storage_uri,
                "checksum": hashlib.sha256(b"image").hexdigest(),
            },
        },
    )
    assert response.status_code == 202, response.text
    intent = await db_session.get(
        ArtifactRefPromotionIntent,
        uuid.UUID(response.json()["promotion_intent_id"]),
    )
    await db_session.refresh(intent)
    assert intent.state == "cleanup"
    assert intent.destination_generation == 21
    assert (
        await db_session.execute(
            select(ArtifactRef).where(ArtifactRef.project_id == project.id)
        )
    ).scalars().all() == []
    await db_session.refresh(receipt)
    assert receipt.artifact_ref_id is None
    assert receipt.consumed_at is None


@pytest.mark.asyncio
async def test_render_promotion_is_deterministic_and_verifies_streamed_checksum(mocker):
    from google.api_core import exceptions as gcs_exceptions

    project_id, tool_id, attachment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    content = b"verified board render"
    source_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project_id}/{attachment_id}/board.png"
    )
    source = mocker.MagicMock()
    source.size = len(content)
    source.generation = 17
    source.open.side_effect = lambda *_args, **_kwargs: nullcontext(
        io.BytesIO(content)
    )
    destination = mocker.MagicMock()
    destination.size = len(content)
    destination.generation = 21
    destination.reload.side_effect = [
        gcs_exceptions.NotFound("absent"),
        None,
        None,
        None,
    ]
    destination.open.side_effect = lambda *_args, **_kwargs: nullcontext(
        io.BytesIO(content)
    )
    bucket = mocker.MagicMock()
    bucket.blob.side_effect = lambda name: (
        source if name.endswith("/board.png") else destination
    )
    bucket.copy_blob.return_value = destination
    storage_client = mocker.MagicMock()
    storage_client.bucket.return_value = bucket
    mocker.patch("app.storage.storage.Client", return_value=storage_client)

    first = await promote_artifact_ref_render(
        source_uri,
        project_id=project_id,
        tool_id=tool_id,
        revision=4,
        attachment_id=attachment_id,
        expected_size_bytes=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    second = await promote_artifact_ref_render(
        source_uri,
        project_id=project_id,
        tool_id=tool_id,
        revision=4,
        attachment_id=attachment_id,
        expected_size_bytes=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert first.storage_uri == second.storage_uri == artifact_ref_render_uri(
        project_id, tool_id, 4, attachment_id
    )
    assert first.created is True
    assert second.created is False
    assert bucket.copy_blob.call_count == 1
    copy_kwargs = bucket.copy_blob.call_args_list[0].kwargs
    assert copy_kwargs["if_source_generation_match"] == 17
    assert copy_kwargs["if_generation_match"] == 0
    assert copy_kwargs["timeout"] == 30
    assert copy_kwargs["retry"] is not None
    destination.delete.assert_not_called()

    source_reload_count = source.reload.call_count
    source.reload.side_effect = gcs_exceptions.NotFound("expired source")
    recovered = await promote_artifact_ref_render(
        source_uri,
        project_id=project_id,
        tool_id=tool_id,
        revision=4,
        attachment_id=attachment_id,
        expected_size_bytes=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert recovered.created is False
    assert recovered.storage_uri == first.storage_uri
    assert source.reload.call_count == source_reload_count
    assert bucket.copy_blob.call_count == 1

    divergent = b"x" * len(content)
    source.size = len(divergent)
    source.open.side_effect = lambda *_args, **_kwargs: nullcontext(
        io.BytesIO(divergent)
    )
    with pytest.raises(StorageUriError, match="checksum"):
        await promote_artifact_ref_render(
            source_uri,
            project_id=project_id,
            tool_id=tool_id,
            revision=5,
            attachment_id=attachment_id,
            expected_size_bytes=len(divergent),
            expected_sha256=hashlib.sha256(divergent).hexdigest(),
        )
    destination.delete.assert_not_called()


@pytest.mark.asyncio
async def test_render_promotion_rejects_source_outside_configured_bucket(mocker):
    storage_client = mocker.patch("app.storage.storage.Client")
    with pytest.raises(StorageUriError, match="outside the artifact bucket"):
        await promote_artifact_ref_render(
            (
                "gs://attacker-artifacts/message-attachments/"
                f"{uuid.uuid4()}/{uuid.uuid4()}/board.png"
            ),
            project_id=uuid.uuid4(),
            tool_id=uuid.uuid4(),
            revision=1,
            attachment_id=uuid.uuid4(),
            expected_size_bytes=5,
            expected_sha256=hashlib.sha256(b"image").hexdigest(),
        )
    storage_client.assert_not_called()


@pytest.mark.asyncio
async def test_promotion_wal_commits_before_effect_and_protects_receipt(
    db_session,
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="whiteboard",
        slug="wal-board",
        title="WAL board",
        content={"strokes": []},
        content_revision=3,
    )
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri="placeholder",
        filename="board.png",
        content_type="image/png",
        expected_size_bytes=17,
        expires_at=_utcnow() + timedelta(seconds=30),
    )
    receipt.storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{receipt.attachment_id}/board.png"
    )
    db_session.add_all([tool, receipt])
    await db_session.commit()
    await db_session.refresh(tool)
    claim = await prepare_artifact_ref_promotion_intent(
        db_session,
        project_id=project.id,
        tool_id=tool.id,
        revision=3,
        receipt_id=receipt.attachment_id,
        actor_user_id=TEST_USER_ID,
        source_storage_uri=receipt.storage_uri,
        candidate_storage_uri=artifact_ref_render_uri(
            project.id, tool.id, 3, receipt.attachment_id
        ),
        expected_size_bytes=17,
        expected_sha256=hashlib.sha256(b"render").hexdigest(),
        render_width=100,
        render_height=80,
    )

    async with AsyncSessionLocal() as observer:
        durable_intent = await observer.get(
            ArtifactRefPromotionIntent, claim.intent_id
        )
        durable_receipt = await observer.get(
            ProjectMessageAttachmentUpload, receipt.attachment_id
        )
        assert durable_intent is not None
        assert durable_intent.state == "copying"
        assert durable_intent.claim_id == claim.claim_id
        assert durable_receipt.expires_at > durable_intent.settle_until


@pytest.mark.asyncio
async def test_second_receipt_waits_on_same_revision_without_forcing_reupload(
    client: AsyncClient, db_session, mocker
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="whiteboard",
        slug="race-board",
        title="Race board",
        content={"strokes": []},
        content_revision=1,
    )
    winner = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri="placeholder",
        filename="winner.png",
        content_type="image/png",
        expected_size_bytes=17,
        expires_at=_utcnow() + timedelta(minutes=20),
    )
    loser = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri="placeholder",
        filename="loser.png",
        content_type="image/png",
        expected_size_bytes=17,
        expires_at=_utcnow() + timedelta(minutes=20),
    )
    winner.storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{winner.attachment_id}/winner.png"
    )
    loser.storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{loser.attachment_id}/loser.png"
    )
    db_session.add_all([tool, winner, loser])
    await db_session.commit()
    await db_session.refresh(tool)
    claim = await prepare_artifact_ref_promotion_intent(
        db_session,
        project_id=project.id,
        tool_id=tool.id,
        revision=1,
        receipt_id=winner.attachment_id,
        actor_user_id=TEST_USER_ID,
        source_storage_uri=winner.storage_uri,
        candidate_storage_uri=artifact_ref_render_uri(
            project.id, tool.id, 1, winner.attachment_id
        ),
        expected_size_bytes=17,
        expected_sha256=hashlib.sha256(b"winner").hexdigest(),
        render_width=100,
        render_height=80,
    )
    reconcile = mocker.patch(
        "app.artifact_ref_promotion.reconcile_one_artifact_ref_promotion",
        new=mocker.AsyncMock(return_value=None),
    )

    body = {
        "expected_revision": 1,
        "rendered_image": {
            "attachment_id": str(loser.attachment_id),
            "storage_uri": loser.storage_uri,
            "checksum": hashlib.sha256(b"loser").hexdigest(),
            "width": 100,
            "height": 80,
        },
    }
    for _ in range(2):
        response = await client.post(
            f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
            json=body,
        )
        assert response.status_code == 202, response.text
        assert response.json()["receipt_id"] == str(loser.attachment_id)
        assert response.json()["promotion_intent_id"] == str(claim.intent_id)
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_upload_reaper_skips_nonterminal_promotion(
    db_session, mocker
):
    project = await _project(db_session)
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=(
            f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
            f"{project.id}/{uuid.uuid4()}/board.png"
        ),
        filename="board.png",
        content_type="image/png",
        expected_size_bytes=17,
        created_at=_utcnow() - timedelta(minutes=10),
        expires_at=_utcnow() - timedelta(seconds=1),
    )
    intent = ArtifactRefPromotionIntent(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        revision=1,
        receipt_id=receipt.attachment_id,
        actor_user_id=TEST_USER_ID,
        source_storage_uri=receipt.storage_uri,
        candidate_storage_uri=(
            f"gs://{ARTIFACT_BUCKET_NAME}/artifact-refs/{project.id}/candidate.png"
        ),
        expected_size_bytes=17,
        expected_sha256="1" * 64,
        state="copying",
        claim_id=uuid.uuid4(),
        lease_expires_at=_utcnow() + timedelta(minutes=10),
        effect_deadline_at=_utcnow() + timedelta(minutes=5),
        settle_until=_utcnow() + timedelta(minutes=15),
    )
    db_session.add_all([receipt, intent])
    await db_session.commit()
    delete_object = mocker.patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=mocker.AsyncMock(return_value=True),
    )

    from app.reaper_sweeps import sweep_expired_message_attachment_uploads

    assert await sweep_expired_message_attachment_uploads(db_session) == 0
    delete_object.assert_not_awaited()
    assert await db_session.get(
        ProjectMessageAttachmentUpload, receipt.attachment_id
    ) is not None


@pytest.mark.asyncio
async def test_attachment_delete_uses_bounded_gcs_deadlines(mocker):
    blob = mocker.MagicMock()
    bucket = mocker.MagicMock()
    bucket.blob.return_value = blob
    client = mocker.MagicMock()
    client.bucket.return_value = bucket
    mocker.patch("app.storage.storage.Client", return_value=client)

    assert await delete_storage_uri_best_effort(
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/project/receipt/file.png"
    )
    kwargs = blob.delete.call_args.kwargs
    assert kwargs["timeout"] == ATTACHMENT_DELETE_RPC_TIMEOUT_SECONDS
    assert kwargs["retry"] is not None
    assert ATTACHMENT_DELETE_RETRY_DEADLINE_SECONDS < 5


@pytest.mark.asyncio
async def test_attachment_delete_is_fenced_by_nonterminal_promotion(
    client: AsyncClient, db_session, mocker
):
    project = await _project(db_session)
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=(
            f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
            f"{project.id}/{uuid.uuid4()}/board.png"
        ),
        filename="board.png",
        content_type="image/png",
        expected_size_bytes=17,
        expires_at=_utcnow() + timedelta(minutes=10),
    )
    intent = ArtifactRefPromotionIntent(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        revision=1,
        receipt_id=receipt.attachment_id,
        actor_user_id=TEST_USER_ID,
        source_storage_uri=receipt.storage_uri,
        candidate_storage_uri=(
            f"gs://{ARTIFACT_BUCKET_NAME}/artifact-refs/{project.id}/candidate.png"
        ),
        expected_size_bytes=17,
        expected_sha256="2" * 64,
        state="copying",
        claim_id=uuid.uuid4(),
        lease_expires_at=_utcnow() + timedelta(minutes=10),
        effect_deadline_at=_utcnow() + timedelta(minutes=5),
        settle_until=_utcnow() + timedelta(minutes=15),
    )
    db_session.add_all([receipt, intent])
    await db_session.commit()
    cleanup = mocker.patch(
        "app.routers.admin_project_strategic_session.delete_storage_uri_best_effort",
        new=mocker.AsyncMock(return_value=True),
    )

    response = await client.delete(
        f"/api/admin/projects/{project.id}/messages/attachment-uploads/"
        f"{receipt.attachment_id}"
    )
    assert response.status_code == 409
    cleanup.assert_not_awaited()
    assert await db_session.get(
        ProjectMessageAttachmentUpload, receipt.attachment_id
    ) is not None


@pytest.mark.asyncio
async def test_attachment_delete_failure_retains_expired_cleanup_evidence(
    client: AsyncClient, db_session, mocker
):
    project = await _project(db_session)
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=(
            f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
            f"{project.id}/{uuid.uuid4()}/orphan.png"
        ),
        filename="orphan.png",
        content_type="image/png",
        expected_size_bytes=17,
        expires_at=_utcnow() + timedelta(minutes=10),
    )
    db_session.add(receipt)
    await db_session.commit()
    cleanup = mocker.patch(
        "app.routers.admin_project_strategic_session.delete_storage_uri_best_effort",
        new=mocker.AsyncMock(return_value=False),
    )

    response = await client.delete(
        f"/api/admin/projects/{project.id}/messages/attachment-uploads/"
        f"{receipt.attachment_id}"
    )
    assert response.status_code == 200
    assert response.json() == {"status": "cleanup_pending", "deleted": False}
    cleanup.assert_awaited_once_with(receipt.storage_uri)
    async with AsyncSessionLocal() as observer:
        retained = await observer.get(
            ProjectMessageAttachmentUpload, receipt.attachment_id
        )
        assert retained is not None
        assert retained.expires_at <= _utcnow()


@pytest.mark.asyncio
async def test_artifact_ref_prefix_cleanup_is_bounded_and_requires_reconciliation(mocker):
    project_id = uuid.uuid4()
    blobs = [mocker.MagicMock() for _ in range(3)]
    storage_client = mocker.MagicMock()
    storage_client.bucket.return_value = mocker.MagicMock()
    storage_client.list_blobs.return_value = blobs
    mocker.patch("app.storage.storage.Client", return_value=storage_client)

    result = await delete_artifact_ref_project_prefix_best_effort(
        project_id,
        max_objects=2,
    )
    assert result == {
        "deleted": 2,
        "complete": False,
        "reconciliation_required": True,
    }
    blobs[0].delete.assert_called_once()
    blobs[1].delete.assert_called_once()
    blobs[2].delete.assert_not_called()
    storage_client.list_blobs.assert_called_once_with(
        storage_client.bucket.return_value,
        prefix=f"artifact-refs/{project_id}/",
        max_results=3,
        timeout=2,
        retry=mocker.ANY,
    )


@pytest.mark.asyncio
async def test_artifact_ref_prefix_cleanup_failure_is_observable(
    mocker, caplog
):
    project_id = uuid.uuid4()
    mocker.patch(
        "app.storage.storage.Client",
        side_effect=RuntimeError("storage unavailable"),
    )

    result = await delete_artifact_ref_project_prefix_best_effort(project_id)
    assert result == {
        "deleted": 0,
        "complete": False,
        "reconciliation_required": True,
    }
    assert any(
        getattr(record, "event", None) == "artifact_ref_cleanup_unknown"
        and getattr(record, "project_id", None) == str(project_id)
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_project_delete_commits_orphan_cleanup_intent(
    client: AsyncClient, db_session
):
    project = await _project(db_session)

    response = await client.delete(f"/api/admin/projects/{project.id}")
    assert response.status_code == 200, response.text
    intent = await db_session.get(ArtifactRefCleanupIntent, project.id)
    assert intent is not None
    assert intent.state == "pending"
    assert intent.object_prefix == f"artifact-refs/{project.id}/"


@pytest.mark.asyncio
async def test_project_delete_fences_late_copy_before_prefix_cleanup(
    client: AsyncClient, db_session, mocker
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="whiteboard",
        slug="delete-race-board",
        title="Delete race board",
        content={"strokes": []},
        content_revision=1,
    )
    receipt = ProjectMessageAttachmentUpload(
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri="placeholder",
        filename="board.png",
        content_type="image/png",
        expected_size_bytes=17,
        expires_at=_utcnow() + timedelta(minutes=20),
    )
    receipt.storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{receipt.attachment_id}/board.png"
    )
    db_session.add_all([tool, receipt])
    await db_session.commit()
    await db_session.refresh(tool)
    claim = await prepare_artifact_ref_promotion_intent(
        db_session,
        project_id=project.id,
        tool_id=tool.id,
        revision=1,
        receipt_id=receipt.attachment_id,
        actor_user_id=TEST_USER_ID,
        source_storage_uri=receipt.storage_uri,
        candidate_storage_uri=artifact_ref_render_uri(
            project.id, tool.id, 1, receipt.attachment_id
        ),
        expected_size_bytes=17,
        expected_sha256="3" * 64,
        render_width=100,
        render_height=80,
    )

    response = await client.delete(f"/api/admin/projects/{project.id}")
    assert response.status_code == 200, response.text
    async with AsyncSessionLocal() as observer:
        assert await observer.get(Project, project.id) is None
        durable = await observer.get(
            ArtifactRefPromotionIntent, claim.intent_id
        )
        assert durable is not None
        assert durable.state == "cleanup"
        assert await observer.get(ArtifactRefCleanupIntent, project.id) is not None
        assert not await record_artifact_ref_promotion_effect(
            observer,
            intent_id=claim.intent_id,
            claim_id=claim.claim_id,
            generation=55,
        )
    assert await _claim_intent(AsyncSessionLocal, project.id) is None

    inspect = mocker.patch(
        "app.artifact_ref_promotion.inspect_artifact_ref_render_candidate",
        new=mocker.AsyncMock(side_effect=[55, None, None]),
    )
    delete_candidate = mocker.patch(
        "app.artifact_ref_promotion.delete_artifact_ref_render_candidate",
        new=mocker.AsyncMock(return_value=True),
    )
    for expected_absent in (0, 1, 2):
        async with AsyncSessionLocal() as updater:
            durable = await updater.get(
                ArtifactRefPromotionIntent, claim.intent_id
            )
            durable.lease_expires_at = _utcnow() - timedelta(seconds=1)
            durable.effect_deadline_at = _utcnow() - timedelta(seconds=1)
            durable.settle_until = _utcnow() - timedelta(seconds=1)
            durable.next_attempt_at = _utcnow() - timedelta(seconds=1)
            updater.add(durable)
            await updater.commit()
        await drain_artifact_ref_promotion_intents(
            session_factory=AsyncSessionLocal,
            limit=1,
        )
        async with AsyncSessionLocal() as observer:
            durable = await observer.get(
                ArtifactRefPromotionIntent, claim.intent_id
            )
            assert durable.settlement_absent_count == expected_absent

    delete_candidate.assert_awaited_once_with(
        artifact_ref_render_uri(project.id, tool.id, 1, receipt.attachment_id),
        generation=55,
    )
    assert inspect.await_count == 3
    async with AsyncSessionLocal() as observer:
        durable = await observer.get(ArtifactRefPromotionIntent, claim.intent_id)
        assert durable.state == "settled"

    prefix_cleanup = mocker.patch(
        "app.artifact_ref_cleanup.delete_artifact_ref_project_prefix_best_effort",
        new=mocker.AsyncMock(return_value={
            "deleted": 0,
            "complete": True,
            "reconciliation_required": False,
        }),
    )
    result = await drain_artifact_ref_cleanup_intents(
        session_factory=AsyncSessionLocal,
        limit=1,
    )
    assert result["completed"] == 1
    prefix_cleanup.assert_awaited_once_with(project.id)


@pytest.mark.asyncio
async def test_cleanup_outbox_retries_incomplete_then_completes(db_session, mocker):
    project_id = uuid.uuid4()
    intent = ArtifactRefCleanupIntent(
        project_id=project_id,
        object_prefix=f"artifact-refs/{project_id}/",
    )
    db_session.add(intent)
    await db_session.commit()
    cleanup = mocker.patch(
        "app.artifact_ref_cleanup.delete_artifact_ref_project_prefix_best_effort",
        new=mocker.AsyncMock(
            side_effect=[
                {"deleted": 10, "complete": False, "reconciliation_required": True},
                {"deleted": 2, "complete": True, "reconciliation_required": False},
            ]
        ),
    )

    first = await drain_artifact_ref_cleanup_intents(
        session_factory=AsyncSessionLocal,
    )
    assert first == {"claimed": 1, "completed": 0, "deferred": 1}
    await db_session.refresh(intent)
    assert intent.state == "pending"
    assert intent.attempt_count == 1
    assert intent.claim_id is None
    intent.next_attempt_at = _utcnow() - timedelta(seconds=1)
    db_session.add(intent)
    await db_session.commit()

    second = await drain_artifact_ref_cleanup_intents(
        session_factory=AsyncSessionLocal,
    )
    assert second == {"claimed": 1, "completed": 1, "deferred": 0}
    await db_session.refresh(intent)
    assert intent.state == "complete"
    assert intent.attempt_count == 2
    assert intent.completed_at is not None
    assert cleanup.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_claim_takeover_fences_late_finisher(db_session):
    project_id = uuid.uuid4()
    intent = ArtifactRefCleanupIntent(
        project_id=project_id,
        object_prefix=f"artifact-refs/{project_id}/",
    )
    db_session.add(intent)
    await db_session.commit()

    first = await _claim_intent(AsyncSessionLocal, project_id)
    assert first is not None
    await db_session.refresh(intent)
    intent.lease_expires_at = _utcnow() - timedelta(seconds=1)
    intent.next_attempt_at = _utcnow() - timedelta(seconds=1)
    db_session.add(intent)
    await db_session.commit()
    second = await _claim_intent(AsyncSessionLocal, project_id)
    assert second is not None
    assert second.claim_id != first.claim_id

    assert await _finish_intent(
        AsyncSessionLocal,
        project_id,
        claim_id=second.claim_id,
        complete=True,
        error=None,
    )
    assert not await _finish_intent(
        AsyncSessionLocal,
        project_id,
        claim_id=first.claim_id,
        complete=False,
        error="late worker",
    )
    await db_session.refresh(intent)
    assert intent.state == "complete"
    assert intent.last_error is None


@pytest.mark.asyncio
async def test_lifecycle_timeout_reuses_one_strong_tracked_promotion_drain(
    monkeypatch,
):
    from app import artifact_ref_cleanup as cleanup_module

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _slow_drain(*, limit):
        nonlocal calls
        calls += 1
        assert limit == 8
        started.set()
        await release.wait()
        return {"claimed": 1, "completed": 1, "deferred": 0}

    cleanup_module._lifecycle_promotion_drain_task = None
    monkeypatch.setattr(
        cleanup_module, "_LIFECYCLE_RECONCILE_WAIT_SECONDS", 0.01
    )
    monkeypatch.setattr(
        cleanup_module, "drain_artifact_ref_promotion_intents", _slow_drain
    )
    assert not await cleanup_module._await_tracked_promotion_drain()
    await started.wait()
    assert calls == 1
    assert not await cleanup_module._await_tracked_promotion_drain()
    assert calls == 1
    release.set()
    await asyncio.sleep(0)
    assert await cleanup_module._await_tracked_promotion_drain()
    assert calls == 1
    assert cleanup_module._lifecycle_promotion_drain_task is None


@pytest.mark.asyncio
async def test_failed_lifecycle_drain_is_cleared_before_retry(monkeypatch):
    from app import artifact_ref_cleanup as cleanup_module

    calls = 0

    async def _drain(*, limit):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        return {"claimed": 0, "completed": 0, "deferred": 0}

    cleanup_module._lifecycle_promotion_drain_task = None
    monkeypatch.setattr(
        cleanup_module, "drain_artifact_ref_promotion_intents", _drain
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await cleanup_module._await_tracked_promotion_drain()
    assert cleanup_module._lifecycle_promotion_drain_task is None
    assert await cleanup_module._await_tracked_promotion_drain()
    assert calls == 2


@pytest.mark.asyncio
async def test_shutdown_cancels_live_replacement_registry_and_monitor():
    from app.lifecycle import _shutdown_background_task_registry

    async def _forever():
        await asyncio.Event().wait()

    replacement = asyncio.create_task(_forever())
    monitor = asyncio.create_task(_forever())
    keepalive = asyncio.create_task(_forever())
    registry = {
        "artifact_ref_lifecycle": replacement,
        "duplicate_identity": replacement,
    }
    await _shutdown_background_task_registry(
        registry,
        monitor_task=monitor,
        leader_keepalive_task=keepalive,
        timeout_seconds=1,
    )
    assert replacement.cancelled()
    assert monitor.cancelled()
    assert keepalive.cancelled()
    assert registry == {}


@pytest.mark.asyncio
async def test_shutdown_timeout_is_bounded_for_noncooperative_monitor():
    from app.lifecycle import _shutdown_background_task_registry

    release = asyncio.Event()

    async def _stubborn_monitor():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    monitor = asyncio.create_task(_stubborn_monitor())
    await asyncio.sleep(0)
    registry: dict[str, asyncio.Task] = {}
    started = asyncio.get_running_loop().time()
    await _shutdown_background_task_registry(
        registry,
        monitor_task=monitor,
        leader_keepalive_task=None,
        timeout_seconds=0.01,
    )
    assert asyncio.get_running_loop().time() - started < 0.5
    assert registry == {}
    assert not monitor.done()
    release.set()
    await monitor


@pytest.mark.asyncio
async def test_artifact_lifecycle_shutdown_clears_shielded_drain():
    from app import artifact_ref_cleanup as cleanup_module

    task = asyncio.create_task(asyncio.Event().wait())
    cleanup_module._lifecycle_promotion_drain_task = task
    await cleanup_module.shutdown_artifact_ref_lifecycle()
    assert task.cancelled()
    assert cleanup_module._lifecycle_promotion_drain_task is None


@pytest.mark.asyncio
async def test_detached_promotion_shutdown_fences_late_database_resume(mocker):
    from app import artifact_ref_promotion as promotion_module

    started = asyncio.Event()
    release = asyncio.Event()

    async def _noncooperative_copy(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return ArtifactRefRenderPromotion(
            storage_uri=(
                f"gs://{ARTIFACT_BUCKET_NAME}/artifact-refs/late-copy.png"
            ),
            created=True,
            generation=73,
        )

    mocker.patch.object(
        promotion_module,
        "promote_artifact_ref_render",
        new=_noncooperative_copy,
    )
    session_factory = mocker.patch.object(
        promotion_module,
        "AsyncSessionLocal",
        side_effect=AssertionError("late promotion reopened the database"),
    )
    task = promotion_module.start_tracked_artifact_ref_promotion(
        intent_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        tool_id=uuid.uuid4(),
        revision=1,
        receipt_id=uuid.uuid4(),
        source_storage_uri=(
            f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/source/render.png"
        ),
        expected_size_bytes=17,
        expected_sha256="4" * 64,
    )
    await started.wait()
    before = asyncio.get_running_loop().time()
    await promotion_module.shutdown_tracked_artifact_ref_promotions(
        timeout_seconds=0.01
    )
    assert asyncio.get_running_loop().time() - before < 0.5
    assert promotion_module._tracked_promotion_tasks == set()
    assert not task.done()
    release.set()
    assert await task is None
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_message_context_accepts_exact_tool_ref_and_rejects_stale_checksum(
    db_session,
):
    project = await _project(db_session)
    tool = ProjectTool(
        project_id=project.id,
        kind="notes",
        slug="notes",
        title="Notes",
        content={"text": "decision"},
        content_revision=1,
    )
    db_session.add(tool)
    await db_session.commit()
    await db_session.refresh(tool)
    ref = await create_tool_artifact_ref(
        db_session,
        project_id=project.id,
        tool_id=tool.id,
        actor_user_id=TEST_USER_ID,
        expected_revision=1,
    )
    await db_session.commit()

    canonical = {
        "id": str(ref.id),
        "artifact_ref_id": str(ref.id),
        "tool_id": str(tool.id),
        "project_id": str(project.id),
        "kind": "notes",
        "revision": 1,
        "checksum": ref.checksum,
        "label": "spoofed label",
        "author": "spoofed-user",
        "retrieval": {
            "capability": "inline_bounded_text",
            "href": f"/api/agent/resident-turn/artifact?artifact_ref_id={ref.id}",
        },
    }
    payload = StrategicSessionMessageCreate(
        content="Use these notes",
        idempotency_key="share-1",
        artifact_refs=[canonical],
    )
    context, _ = await _validated_message_scoped_context(
        db_session, project_id=project.id, payload=payload
    )
    assert context["artifact_refs"][0]["artifact_ref_id"] == str(ref.id)
    assert context["artifact_refs"][0] == artifact_ref_projection(ref)
    projected, omitted = _project_revisioned_refs(
        context["artifact_refs"], project_id=project.id
    )
    assert omitted == 0
    assert projected[0]["retrieval"]["capability"] == "inline_bounded_text"
    assert projected[0]["artifact_ref_id"] == str(ref.id)

    payload.artifact_refs[0]["checksum"] = "0" * 64
    with pytest.raises(Exception) as caught:
        await _validated_message_scoped_context(
            db_session, project_id=project.id, payload=payload
        )
    assert getattr(caught.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_legacy_artifact_kind_notes_still_uses_artifact_id(db_session):
    project = await _project(db_session)
    task = Task(project_id=project.id, title="Legacy notes artifact")
    db_session.add(task)
    await db_session.flush()
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        iteration=2,
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    payload = StrategicSessionMessageCreate(
        content="Use legacy evidence",
        idempotency_key="legacy-notes-artifact",
        artifact_refs=[{
            "kind": "notes",
            "artifact_id": str(artifact.id),
            "revision": 2,
        }],
    )
    context, _ = await _validated_message_scoped_context(
        db_session, project_id=project.id, payload=payload
    )
    assert context["artifact_refs"][0]["artifact_id"] == str(artifact.id)


def test_artifact_ref_model_is_registered():
    assert ArtifactRef.__tablename__ == "artifactref"
    receipt_indexes = {
        index.name for index in ProjectMessageAttachmentUpload.__table__.indexes
    }
    assert "ix_projectmessageattachmentupload_artifact_ref_id" in receipt_indexes


def _resident_token(task: Task) -> dict:
    return {
        "sub": str(task.id),
        "project_id": str(task.project_id),
        "vm_instance_id": "runner:test",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce",
    }


def _rate_request(*, token: str, remote: str = "10.0.0.5"):
    from starlette.requests import Request

    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/agent/resident-turn/artifact",
        "headers": [(b"x-agent-token", token.encode())],
        "client": (remote, 443),
        "server": ("test", 80),
        "scheme": "http",
        "query_string": b"",
    })


def test_resident_agent_rate_keys_limit_token_and_remote_independently():
    first = _rate_request(token="task-token-a")
    same = _rate_request(token="task-token-a")
    rotated = _rate_request(token="task-token-b")
    other_remote = _rate_request(token="task-token-a", remote="10.0.0.6")

    assert resident_task_token_rate_key(first) == resident_task_token_rate_key(same)
    assert resident_task_token_rate_key(first) != resident_task_token_rate_key(rotated)
    assert resident_remote_rate_key(first) == resident_remote_rate_key(rotated)
    assert resident_remote_rate_key(first) != resident_remote_rate_key(other_remote)


def test_artifact_retrieval_budget_counts_repeated_signing_requests():
    artifact_ref_id = uuid.uuid4()
    task = Task(project_id=uuid.uuid4(), context={})
    for _ in range(MAX_ARTIFACT_RETRIEVALS_PER_TURN):
        assert _register_artifact_retrieval(task, artifact_ref_id)
    with pytest.raises(HTTPException) as exhausted:
        _register_artifact_retrieval(task, artifact_ref_id)
    assert exhausted.value.status_code == 429


@pytest.mark.asyncio
async def test_resident_artifact_retrieval_denies_unattached_and_cross_project(
    db_session, mocker
):
    project = await _project(db_session)
    foreign = await _project(db_session, user_id="other-user")
    own_ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=1,
        checksum="1" * 64,
        content_snapshot={"text": "own"},
    )
    foreign_ref = ArtifactRef(
        project_id=foreign.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=1,
        checksum="2" * 64,
        content_snapshot={"text": "foreign"},
    )
    task = Task(
        project_id=project.id,
        context={RESIDENT_ARTIFACT_REF_GRANTS_KEY: []},
    )
    db_session.add_all([own_ref, foreign_ref, task])
    await db_session.commit()

    async def _load(*args, **kwargs):
        return task

    mocker.patch(
        "app.routers.agent_resident_artifacts.load_live_resident_agent_task_locked",
        new=_load,
    )
    with pytest.raises(HTTPException) as unattached:
        await get_resident_turn_artifact(
            None, own_ref.id, db_session, _resident_token(task)
        )
    assert unattached.value.status_code == 404

    task.context = {
        RESIDENT_ARTIFACT_REF_GRANTS_KEY: [str(foreign_ref.id)]
    }
    with pytest.raises(HTTPException) as cross_project:
        await get_resident_turn_artifact(
            None, foreign_ref.id, db_session, _resident_token(task)
        )
    assert cross_project.value.status_code == 404


@pytest.mark.asyncio
async def test_message_route_freezes_tool_ref_before_message_insert(
    client: AsyncClient, db_session, mocker
):
    project = await _project(db_session)
    resident = await initialize_strategic_resident(db_session, project.id)
    resident.engine_owner = "strategic_resident"
    tool = ProjectTool(
        project_id=project.id,
        kind="notes",
        slug="notes",
        title="Notes",
        content={"text": "decisive evidence"},
        content_revision=1,
    )
    db_session.add_all([resident, tool])
    await db_session.commit()
    await db_session.refresh(tool)
    ref = await create_tool_artifact_ref(
        db_session,
        project_id=project.id,
        tool_id=tool.id,
        actor_user_id=TEST_USER_ID,
        expected_revision=1,
    )
    await db_session.commit()

    response = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/messages",
        json={
            "message_type": "NOTE",
            "content": "Use the exact Notes revision.",
            "artifact_refs": [artifact_ref_projection(ref)],
            "idempotency_key": "production-order-artifact-grant",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["wakeup_state"] in {"queued", "waiting_for_capacity"}
    await db_session.refresh(resident)
    task = await db_session.get(Task, resident.active_turn_task_id)
    assert task is not None
    assert task.context[RESIDENT_ARTIFACT_REF_GRANTS_KEY] == [str(ref.id)]

    async def _load(*args, **kwargs):
        return task

    mocker.patch(
        "app.routers.agent_resident_artifacts.load_live_resident_agent_task_locked",
        new=_load,
    )
    retrieved = await get_resident_turn_artifact(
        None, ref.id, db_session, _resident_token(task)
    )
    assert retrieved["content"]["text"] == "decisive evidence"


@pytest.mark.asyncio
async def test_whiteboard_signing_runs_after_authority_locks_release(db_session, mocker):
    project = await _project(db_session)
    ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="whiteboard",
        revision=1,
        checksum="3" * 64,
        content_snapshot={"stroke_count": 1},
        retrieval_metadata={
            "capability": "project_tool_rendered_image",
            "storage_uri": "gs://bucket/board.png",
        },
    )
    task = Task(
        project_id=project.id,
        context={RESIDENT_ARTIFACT_REF_GRANTS_KEY: [str(ref.id)]},
    )
    db_session.add_all([ref, task])
    await db_session.commit()

    async def _load(*args, **kwargs):
        return task

    async def _sign(*args, **kwargs):
        assert not db_session.in_transaction()
        return "https://signed.example/board.png"

    mocker.patch(
        "app.routers.agent_resident_artifacts.load_live_resident_agent_task_locked",
        new=_load,
    )
    mocker.patch(
        "app.routers.agent_resident_artifacts.generate_download_signed_url",
        new=_sign,
    )
    response = await get_resident_turn_artifact(
        None, ref.id, db_session, _resident_token(task)
    )
    assert response["retrieval"]["download_url"].startswith("https://signed.example")
