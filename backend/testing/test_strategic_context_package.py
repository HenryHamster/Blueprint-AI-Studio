"""Tests for strategic_context_package.py read-model functions."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func
from sqlmodel import select

from app.models import Artifact, KnowledgeDocument, Project, Task
from app.strategic_context_package import (
    _artifact_descriptor,
    _artifact_renderable_kind,
    load_strategic_context_evidence,
)

TEST_USER_ID = "test-user-123"


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_pending_count_is_real_aggregate(db_session):
    """pending_count must reflect ALL pending docs, not just the top-4 sample slice."""
    project = Project(name="KdTest", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="source",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Create 6 pending documents — more than the .limit(4) in the items query,
    # so the OLD code would have reported at most 4 instead of 6.
    for i in range(6):
        db_session.add(
            KnowledgeDocument(
                project_id=project.id,
                source_task_id=task.id,
                title=f"Pending doc {i}",
                summary=f"Summary {i}",
                body=f"Body {i}",
                lifecycle_status="pending",
            )
        )
    # Also add 2 consumed and 1 dismissed doc that should NOT count as pending.
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Consumed doc",
            summary="Consumed",
            body="Body consumed",
            lifecycle_status="consumed",
        )
    )
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Consumed doc 2",
            summary="Consumed 2",
            body="Body consumed 2",
            lifecycle_status="consumed",
        )
    )
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Dismissed doc",
            summary="Dismissed",
            body="Body dismissed",
            lifecycle_status="dismissed",
        )
    )
    await db_session.commit()

    # Verify total document count
    total_in_db = (
        await db_session.execute(
            select(func.count()).where(KnowledgeDocument.project_id == project.id)
        )
    ).scalar()
    assert total_in_db == 9  # 6 pending + 2 consumed + 1 dismissed

    result = await load_strategic_context_evidence(db_session, project.id)
    knowledge = result["knowledge"]

    # The corrected pending_count must be 6 (all pending docs), not capped at 4.
    assert knowledge["pending_count"] == 6, (
        f"pending_count should reflect all pending docs (6), got {knowledge['pending_count']}"
    )

    # Total must be 9
    assert knowledge["total"] == 9

    # Items list is still a preview (top-N limited sample), showing only pending docs.
    # Even though 6 docs are pending, only up to 4 appear in the items list.
    assert len(knowledge["items"]) <= 4
    for item in knowledge["items"]:
        assert item["lifecycle_status"] == "pending", (
            f"Non-pending doc in items: {item.get('id')}"
        )

    # Staleness metric must be present and non-None since there are pending docs.
    assert knowledge["oldest_pending_age_hours"] is not None
    assert knowledge["oldest_pending_age_hours"] >= 0


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_no_pending_docs(db_session):
    """When no pending docs exist, pending_count is 0 and staleness is None."""
    project = Project(name="KdTestEmpty", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="source",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Only consumed / dismissed docs — no pending.
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Done",
            summary="Done",
            body="Body",
            lifecycle_status="consumed",
        )
    )
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Dismissed",
            summary="Dismissed",
            body="Body",
            lifecycle_status="dismissed",
        )
    )
    await db_session.commit()

    result = await load_strategic_context_evidence(db_session, project.id)
    knowledge = result["knowledge"]

    assert knowledge["pending_count"] == 0
    assert knowledge["total"] == 2
    assert knowledge["items"] == []
    assert knowledge["oldest_pending_age_hours"] is None


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_staleness_reflects_oldest_pending(db_session):
    """oldest_pending_age_hours should reflect the age of the earliest-created pending doc."""
    project = Project(name="KdTestStale", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="source",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    now = datetime.utcnow()
    # Create a doc from 72 hours ago — should be the oldest.
    old_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        title="Old pending",
        summary="Very old",
        body="Body old",
        lifecycle_status="pending",
        created_at=now - timedelta(hours=72),
        updated_at=now - timedelta(hours=72),
    )
    db_session.add(old_doc)
    # Recent doc — created just now.
    new_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        title="Recent pending",
        summary="Recent",
        body="Body recent",
        lifecycle_status="pending",
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    db_session.add(new_doc)
    await db_session.commit()

    result = await load_strategic_context_evidence(db_session, project.id)
    knowledge = result["knowledge"]

    assert knowledge["pending_count"] == 2
    assert knowledge["oldest_pending_age_hours"] is not None
    # The oldest doc was created ~72 hours ago, so the staleness should be near 72.
    assert knowledge["oldest_pending_age_hours"] >= 70
    assert knowledge["oldest_pending_age_hours"] <= 74


# ── viewable-artifact preview URL tests ──────────────────────────────────


def test_artifact_renderable_kind_image():
    """Artifact with image tokens yields kind='image'."""
    assert _artifact_renderable_kind({"artifact_type": "COMPUTE_RESULT", "logical_key": "plot.png"}) == "image"
    assert _artifact_renderable_kind({"artifact_type": "image", "label": "chart"}) == "image"
    assert _artifact_renderable_kind({"kind": "screenshot.png"}) == "image"


def test_artifact_renderable_kind_video():
    """Artifact with video tokens yields kind='video'."""
    assert _artifact_renderable_kind({"artifact_type": "COMPUTE_RESULT", "logical_key": "output.mp4"}) == "video"
    assert _artifact_renderable_kind({"artifact_type": "COMPUTE_RESULT", "logical_key": "demo.webm"}) == "video"
    assert _artifact_renderable_kind({"kind": "video", "label": "result"}) == "video"


def test_artifact_renderable_kind_fallback():
    """Unknown artifact tokens fall back to resource_detail."""
    assert _artifact_renderable_kind({"artifact_type": "UNKNOWN"}) == "resource_detail"
    assert _artifact_renderable_kind({}) == "resource_detail"


def test_artifact_descriptor_image_with_preview_url():
    """Image artifact with a viewable_preview populates image.url and drops unsupported_reason."""
    item = {
        "id": "art-1",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "plot.png",
        "label": "Plot",
        "task_title": "Training run",
        "task_status": "completed",
        "viewable_previews": [
            {"url": "https://signed.example.com/plot.png?sig=abc", "kind": "image", "filename": "plot.png"},
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "image"
    assert desc["image"]["url"] == "https://signed.example.com/plot.png?sig=abc"
    assert "unsupported_reason" not in desc


def test_artifact_descriptor_image_no_preview():
    """Image artifact without viewable_previews keeps the existing stub."""
    item = {
        "id": "art-2",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "plot.png",
        "label": "Plot",
        "task_title": "Training run",
        "task_status": "completed",
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "image"
    assert desc["image"]["url"] is None
    assert "unsupported_reason" in desc


def test_artifact_descriptor_video_with_preview_url():
    """Video artifact with a viewable_preview populates video.url and drops unsupported_reason."""
    item = {
        "id": "art-3",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "demo.mp4",
        "label": "Demo",
        "task_title": "Inference run",
        "task_status": "completed",
        "viewable_previews": [
            {"url": "https://signed.example.com/demo.mp4?sig=def", "kind": "video", "filename": "demo.mp4"},
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "video"
    assert desc["video"]["url"] == "https://signed.example.com/demo.mp4?sig=def"
    assert "unsupported_reason" not in desc


def test_artifact_descriptor_video_no_preview():
    """Video artifact without viewable_previews returns stub with unsupported_reason."""
    item = {
        "id": "art-4",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "demo.mp4",
        "label": "Demo",
        "task_title": "Inference run",
        "task_status": "completed",
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "video"
    assert desc["video"]["url"] is None
    assert "unsupported_reason" in desc


def test_artifact_descriptor_multi_image_populates_images_list():
    """Image artifact with multiple viewable_previews populates both image (first) and images (all)."""
    item = {
        "id": "art-5",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "results/",
        "label": "Training Plots",
        "task_title": "Multi-arm run",
        "task_status": "completed",
        "viewable_previews": [
            {"url": "https://signed.example.com/arm0/reward.png?sig=a1", "kind": "image", "filename": "arm0/reward.png"},
            {"url": "https://signed.example.com/arm1/reward.png?sig=a2", "kind": "image", "filename": "arm1/reward.png"},
            {"url": "https://signed.example.com/arm2/reward.png?sig=a3", "kind": "image", "filename": "arm2/reward.png"},
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "image"
    assert desc["image"]["url"] == "https://signed.example.com/arm0/reward.png?sig=a1"
    assert "unsupported_reason" not in desc
    assert len(desc["images"]) == 3
    assert desc["images"][0]["url"] == "https://signed.example.com/arm0/reward.png?sig=a1"
    assert desc["images"][1]["url"] == "https://signed.example.com/arm1/reward.png?sig=a2"
    assert desc["images"][2]["url"] == "https://signed.example.com/arm2/reward.png?sig=a3"
    assert desc["images"][0]["filename"] == "arm0/reward.png"


def test_artifact_descriptor_single_image_also_populates_images_list():
    """Single-image artifact also emits images list for API consistency."""
    item = {
        "id": "art-6",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "plot.png",
        "label": "Plot",
        "task_title": "Run",
        "task_status": "completed",
        "viewable_previews": [
            {"url": "https://signed.example.com/plot.png?sig=abc", "kind": "image", "filename": "plot.png"},
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert len(desc["images"]) == 1
    assert desc["images"][0]["url"] == "https://signed.example.com/plot.png?sig=abc"


def test_artifact_descriptor_multi_video_populates_videos_list():
    """Video artifact with multiple viewable_previews populates both video (first) and videos (all)."""
    item = {
        "id": "art-7",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "clips/",
        "label": "Training Clips",
        "task_title": "Video run",
        "task_status": "completed",
        "viewable_previews": [
            {"url": "https://signed.example.com/clip0.mp4?sig=v1", "kind": "video", "filename": "clip0.mp4"},
            {"url": "https://signed.example.com/clip1.mp4?sig=v2", "kind": "video", "filename": "clip1.mp4"},
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "video"
    assert desc["video"]["url"] == "https://signed.example.com/clip0.mp4?sig=v1"
    assert "unsupported_reason" not in desc
    assert len(desc["videos"]) == 2
    assert desc["videos"][0]["url"] == "https://signed.example.com/clip0.mp4?sig=v1"
    assert desc["videos"][1]["url"] == "https://signed.example.com/clip1.mp4?sig=v2"
    assert desc["videos"][0]["filename"] == "clip0.mp4"


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_artifact_viewable_files(db_session):
    """An artifact with viewable_files in its logs yields viewable_previews with signed URLs."""
    project = Project(name="ViewableTest", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute task",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = {
        "compute_job_id": "job-1",
        "viewable_files": [
            {"filename": "plot.png", "storage_uri": "gs://bucket/obj1", "kind": "image", "content_type": "image/png"},
            {"filename": "video.mp4", "storage_uri": "gs://bucket/obj2", "kind": "video", "content_type": "video/mp4"},
        ],
    }
    artifact = Artifact(
        task_id=task.id,
        artifact_type="COMPUTE_RESULT",
        storage_uri="gs://bucket/result",
        logs=json.dumps(logs),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    with (
        patch("app.strategic_context_package.verify_artifact_project_ownership") as mock_verify,
        patch("app.strategic_context_package.generate_download_signed_url") as mock_sign,
    ):
        mock_verify.return_value = None  # ownership check passes
        mock_sign.side_effect = [
            "https://signed-url/plot.png?token=abc",
            "https://signed-url/video.mp4?token=def",
        ]
        result = await load_strategic_context_evidence(db_session, project.id)

    artifacts = result["artifacts"]["items"]
    assert len(artifacts) == 1
    item = artifacts[0]
    assert item["storage_uri"] == "gs://bucket/result"
    assert "viewable_previews" in item
    assert len(item["viewable_previews"]) == 2
    assert item["viewable_previews"][0] == {
        "url": "https://signed-url/plot.png?token=abc",
        "kind": "image",
        "filename": "plot.png",
    }
    assert item["viewable_previews"][1] == {
        "url": "https://signed-url/video.mp4?token=def",
        "kind": "video",
        "filename": "video.mp4",
    }
    assert mock_verify.call_count == 2
    assert mock_sign.call_count == 2


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_artifact_ownership_failure_skips_file(db_session):
    """When ownership verification fails for a viewable file, that file is skipped."""
    project = Project(name="OwnFail", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute task",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = {
        "compute_job_id": "job-2",
        "viewable_files": [
            {"filename": "safe.png", "storage_uri": "gs://bucket/safe", "kind": "image"},
            {"filename": "evil.png", "storage_uri": "gs://bucket/evil", "kind": "image"},
        ],
    }
    artifact = Artifact(
        task_id=task.id,
        artifact_type="COMPUTE_RESULT",
        storage_uri="gs://bucket/result",
        logs=json.dumps(logs),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    with (
        patch("app.strategic_context_package.verify_artifact_project_ownership") as mock_verify,
        patch("app.strategic_context_package.generate_download_signed_url") as mock_sign,
    ):
        # First file passes, second file fails ownership
        mock_verify.side_effect = [None, Exception("ownership violation")]
        mock_sign.return_value = "https://signed-url/safe.png?token=abc"
        result = await load_strategic_context_evidence(db_session, project.id)

    artifacts = result["artifacts"]["items"]
    assert len(artifacts) == 1
    item = artifacts[0]
    assert "viewable_previews" in item
    assert len(item["viewable_previews"]) == 1
    assert item["viewable_previews"][0]["filename"] == "safe.png"
    assert mock_verify.call_count == 2
    assert mock_sign.call_count == 1


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_artifact_no_logs(db_session):
    """An artifact with no logs builds the item dict without viewable_previews."""
    project = Project(name="NoLogs", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="plain task",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type="COMPUTE_RESULT",
        storage_uri="gs://bucket/result",
        logs=None,
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    result = await load_strategic_context_evidence(db_session, project.id)
    artifacts = result["artifacts"]["items"]
    assert len(artifacts) == 1
    item = artifacts[0]
    assert item["storage_uri"] == "gs://bucket/result"
    assert "viewable_previews" not in item
