"""Tests for strategic_context_package.py read-model functions."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import func
from sqlmodel import select

import app.strategic_context_package as scp
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobStatus,
    KnowledgeDocument,
    Project,
    Task,
)
from app.storage import ARTIFACT_BUCKET_NAME
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

    now = datetime.now(timezone.utc)
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


# ── Viewable artifact previews (signed at the async load boundary) ───────────


async def _make_project_task(db_session, name="PreviewTest"):
    project = Project(name=name, user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="experiment",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


def _fake_signer(calls=None):
    async def fake(storage_uri, expiration_minutes=60):
        if calls is not None:
            calls.append((storage_uri, expiration_minutes))
        return f"https://signed.example/{storage_uri.rsplit('/', 1)[-1]}"

    return fake


@pytest.mark.asyncio
async def test_artifact_image_preview_signed_at_load(db_session, monkeypatch):
    """An Artifact whose own storage_uri is an allowlisted image gets a signed,
    transient viewable_previews entry minted inside
    load_strategic_context_evidence, and the descriptor renders it (no more
    unsupported_reason stub)."""
    project, task = await _make_project_task(db_session)
    image_uri = f"gs://{ARTIFACT_BUCKET_NAME}/tasks/{task.id}/loss_curve.png"
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.SCREENSHOT,
            storage_uri=image_uri,
        )
    )
    await db_session.commit()

    calls: list = []
    monkeypatch.setattr(scp, "generate_download_signed_url", _fake_signer(calls))

    evidence = await load_strategic_context_evidence(db_session, project.id)
    items = evidence["artifacts"]["items"]
    assert len(items) == 1
    item = items[0]
    # No-leak contract (test_strategic_state.py): the raw gs:// reference must
    # never appear in the package — only the signed, transient URL does.
    assert "storage_uri" not in item
    previews = item["viewable_previews"]
    assert previews == [
        {
            "url": "https://signed.example/loss_curve.png",
            "storage_uri": image_uri,
            "kind": "image",
            "filename": "loss_curve.png",
        }
    ]
    assert calls == [(image_uri, scp._PREVIEW_SIGNED_URL_MINUTES)]

    descriptor = _artifact_descriptor(item)
    assert descriptor["kind"] == "image"
    assert descriptor["image"]["url"] == "https://signed.example/loss_curve.png"
    # storage_uri now rides on the descriptor sub-dict for lazy re-resolution;
    # the no-leak contract is enforced at the item level (asserted above).
    assert descriptor["image"]["storage_uri"] == image_uri
    assert "unsupported_reason" not in descriptor


@pytest.mark.asyncio
async def test_bundle_artifact_resolves_manifest_viewable_video(
    db_session, monkeypatch
):
    """A compute-job bundle Artifact resolves through its logs' viewable_files
    manifest; entries failing the per-file ownership check are never signed,
    and a resolved video preview promotes the descriptor kind to video."""
    project, task = await _make_project_task(db_session, name="PreviewVideo")

    own_video = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "11111111-1111-1111-1111-111111111111/artifacts/viewable/rollout.mp4"
    )
    foreign = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "00000000-0000-0000-0000-000000000001/artifacts/viewable/evil.png"
    )
    bundle_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "11111111-1111-1111-1111-111111111111/artifacts/bundle.tar.gz"
    )
    logs = {
        "viewable_files": [
            # Foreign entry listed FIRST: ownership check fails, never signed.
            {"filename": "evil.png", "storage_uri": foreign, "kind": "image"},
            {"filename": "rollout.mp4", "storage_uri": own_video, "kind": "video"},
        ]
    }
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.DEBUG_RESULT,
            storage_uri=bundle_uri,
            logs=json.dumps(logs),
        )
    )
    await db_session.commit()

    async def fake_verify(storage_uri, project_id, session):
        if storage_uri == foreign:
            raise Exception("ownership violation")

    calls: list = []
    monkeypatch.setattr(scp, "verify_artifact_project_ownership", fake_verify)
    monkeypatch.setattr(scp, "generate_download_signed_url", _fake_signer(calls))

    evidence = await load_strategic_context_evidence(db_session, project.id)
    item = evidence["artifacts"]["items"][0]
    # No-leak contract: raw gs:// never rides in the package payload; the
    # signing-call record below proves WHICH object was resolved instead.
    assert "storage_uri" not in item
    previews = item["viewable_previews"]
    assert previews == [
        {
            "url": "https://signed.example/rollout.mp4",
            "storage_uri": own_video,
            "kind": "video",
            "filename": "rollout.mp4",
        }
    ]
    # Only the owned object was ever signed — the foreign entry was skipped.
    assert calls == [(own_video, scp._PREVIEW_SIGNED_URL_MINUTES)]

    descriptor = _artifact_descriptor(item)
    assert descriptor["kind"] == "video"
    assert descriptor["video"]["url"] == "https://signed.example/rollout.mp4"
    # storage_uri now rides on the descriptor sub-dict for lazy re-resolution;
    # the no-leak contract is enforced at the item level (asserted above).
    assert descriptor["video"]["storage_uri"] == own_video


@pytest.mark.asyncio
async def test_viewable_revision_reaches_signed_preview_and_descriptor(
    db_session, monkeypatch
):
    """Revision/live metadata survives signing without changing the media URI."""
    project, task = await _make_project_task(db_session, name="RevisionPreview")
    plot_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "11111111-1111-1111-1111-111111111111/artifacts/viewable/reward.png"
    )
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.DEBUG_RESULT,
            storage_uri=(
                f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
                "11111111-1111-1111-1111-111111111111/artifacts/bundle.tar.gz"
            ),
            logs=json.dumps(
                {
                    "viewable_files": [
                        {
                            "filename": "reward.png",
                            "storage_uri": plot_uri,
                            "kind": "image",
                            "revision": "terminal-revision-9",
                            "live": False,
                        }
                    ]
                }
            ),
        )
    )
    await db_session.commit()

    async def fake_verify(storage_uri, project_id, session):
        assert storage_uri == plot_uri

    monkeypatch.setattr(scp, "verify_artifact_project_ownership", fake_verify)
    monkeypatch.setattr(scp, "generate_download_signed_url", _fake_signer([]))

    evidence = await load_strategic_context_evidence(db_session, project.id)
    preview = evidence["artifacts"]["items"][0]["viewable_previews"][0]
    assert preview["revision"] == "terminal-revision-9"
    assert preview["live"] is False
    descriptor = _artifact_descriptor(evidence["artifacts"]["items"][0])
    assert descriptor["image"]["revision"] == "terminal-revision-9"
    assert descriptor["images"][0]["live"] is False


@pytest.mark.asyncio
async def test_signing_failure_falls_back_to_stub(db_session, monkeypatch):
    """Fail-soft: a signing error yields the pre-existing metadata-only stub."""
    project, task = await _make_project_task(db_session, name="PreviewFail")
    image_uri = f"gs://{ARTIFACT_BUCKET_NAME}/tasks/{task.id}/plot.png"
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.SCREENSHOT,
            storage_uri=image_uri,
            logical_key="plot.png",
        )
    )
    await db_session.commit()

    async def broken_signer(storage_uri, expiration_minutes=60):
        raise RuntimeError("storage outage")

    monkeypatch.setattr(scp, "generate_download_signed_url", broken_signer)

    evidence = await load_strategic_context_evidence(db_session, project.id)
    item = evidence["artifacts"]["items"][0]
    assert "viewable_previews" not in item

    descriptor = _artifact_descriptor(item)
    assert descriptor["kind"] == "image"
    assert descriptor["image"] == {
        "url": None,
        "alt": descriptor["title"],
        "storage_uri": None,
    }
    assert "unsupported_reason" in descriptor


@pytest.mark.asyncio
async def test_svg_is_never_previewable(db_session, monkeypatch):
    """svg is excluded from the viewable allowlist (script execution via a
    signed URL) — no preview and no signing call, whether the svg is the
    artifact's own storage_uri or a stamped manifest entry."""
    project, task = await _make_project_task(db_session, name="PreviewSvg")
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.SCREENSHOT,
            storage_uri=f"gs://{ARTIFACT_BUCKET_NAME}/tasks/{task.id}/chart.svg",
            logs=json.dumps(
                {
                    "viewable_files": [
                        {
                            "filename": "diagram.svg",
                            "storage_uri": f"gs://{ARTIFACT_BUCKET_NAME}/tasks/{task.id}/diagram.svg",
                            "kind": "image",
                        }
                    ]
                }
            ),
        )
    )
    await db_session.commit()

    verify_calls: list = []

    async def fake_verify(storage_uri, project_id, session):
        verify_calls.append(storage_uri)

    calls: list = []
    monkeypatch.setattr(scp, "verify_artifact_project_ownership", fake_verify)
    monkeypatch.setattr(scp, "generate_download_signed_url", _fake_signer(calls))

    evidence = await load_strategic_context_evidence(db_session, project.id)
    item = evidence["artifacts"]["items"][0]
    assert "viewable_previews" not in item
    assert calls == []
    assert verify_calls == []


@pytest.mark.asyncio
async def test_running_compute_job_midrun_previews_populate_descriptor(
    db_session, monkeypatch
):
    """A RUNNING compute job whose polling_metadata carries mid-run viewable
    files is surfaced as an evidence artifact with signed viewable_previews, so
    the strategic-canvas descriptor renders inline (kind=image, populated URL,
    no unsupported_reason stub) before the job terminates — and no raw
    storage_uri leaks onto the item."""
    project, task = await _make_project_task(db_session, name="RunningJob")

    plot_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "22222222-2222-2222-2222-222222222222/artifacts/viewable/reward_curve.png"
    )
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local_process",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={
            "viewable_files": [
                {
                    "filename": "reward_curve.png",
                    "storage_uri": plot_uri,
                    "kind": "image",
                    "content_type": "image/png",
                    "size_bytes": 1024,
                }
            ]
        },
    )
    db_session.add(job)
    await db_session.commit()

    async def fake_verify(storage_uri, project_id, session):
        return None

    calls: list = []
    monkeypatch.setattr(scp, "verify_artifact_project_ownership", fake_verify)
    monkeypatch.setattr(scp, "generate_download_signed_url", _fake_signer(calls))

    evidence = await load_strategic_context_evidence(db_session, project.id)
    items = evidence["artifacts"]["items"]
    assert len(items) == 1
    item = items[0]
    # No-leak contract: raw gs:// never rides on the item itself.
    assert "storage_uri" not in item
    assert item["compute_job_running"] is True
    assert item["task_status"] == "RUNNING"
    assert item["viewable_previews"] == [
        {
            "url": "https://signed.example/reward_curve.png",
            "storage_uri": plot_uri,
            "kind": "image",
            "filename": "reward_curve.png",
        }
    ]
    assert calls == [(plot_uri, scp._PREVIEW_SIGNED_URL_MINUTES)]

    descriptor = _artifact_descriptor(item)
    assert descriptor["kind"] == "image"
    assert descriptor["image"]["url"] == "https://signed.example/reward_curve.png"
    assert "unsupported_reason" not in descriptor


@pytest.mark.asyncio
async def test_terminal_linked_compute_job_not_double_surfaced(
    db_session, monkeypatch
):
    """A compute job already linked to a durable Artifact (artifact_id set) is
    NOT re-surfaced by the running-job pass, so a terminal job's previews come
    only from its Artifact — no duplicate live item."""
    project, task = await _make_project_task(db_session, name="TerminalJob")

    plot_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "33333333-3333-3333-3333-333333333333/artifacts/viewable/final.png"
    )
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.DEBUG_RESULT,
        storage_uri=f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/x/bundle.tar.gz",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    # A SUCCEEDED job linked to that artifact — still carries a viewable_files
    # manifest in polling_metadata, but must not be double-surfaced.
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local_process",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=artifact.id,
        polling_metadata={
            "viewable_files": [
                {"filename": "final.png", "storage_uri": plot_uri, "kind": "image"}
            ]
        },
    )
    db_session.add(job)
    await db_session.commit()

    async def fake_verify(storage_uri, project_id, session):
        return None

    calls: list = []
    monkeypatch.setattr(scp, "verify_artifact_project_ownership", fake_verify)
    monkeypatch.setattr(scp, "generate_download_signed_url", _fake_signer(calls))

    evidence = await load_strategic_context_evidence(db_session, project.id)
    items = evidence["artifacts"]["items"]
    # Exactly one item — the durable Artifact. No live running-job duplicate.
    assert len(items) == 1
    assert all(not it.get("compute_job_running") for it in items)


@pytest.mark.asyncio
async def test_running_job_manifest_ownership_and_svg_filtered(
    db_session, monkeypatch
):
    """The running-job pass reuses the shared signer: a foreign-owned entry is
    skipped and an svg entry is never signed, so only the owned allowlisted
    file yields a preview."""
    project, task = await _make_project_task(db_session, name="RunningFilter")

    own_video = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "44444444-4444-4444-4444-444444444444/artifacts/viewable/rollout.mp4"
    )
    foreign = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "00000000-0000-0000-0000-000000000009/artifacts/viewable/evil.png"
    )
    svg = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "44444444-4444-4444-4444-444444444444/artifacts/viewable/diagram.svg"
    )
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local_process",
        status=ComputeJobStatus.POLLING,
        polling_metadata={
            "viewable_files": [
                {"filename": "evil.png", "storage_uri": foreign, "kind": "image"},
                {"filename": "diagram.svg", "storage_uri": svg, "kind": "image"},
                {"filename": "rollout.mp4", "storage_uri": own_video, "kind": "video"},
            ]
        },
    )
    db_session.add(job)
    await db_session.commit()

    async def fake_verify(storage_uri, project_id, session):
        if storage_uri == foreign:
            raise Exception("ownership violation")

    calls: list = []
    monkeypatch.setattr(scp, "verify_artifact_project_ownership", fake_verify)
    monkeypatch.setattr(scp, "generate_download_signed_url", _fake_signer(calls))

    evidence = await load_strategic_context_evidence(db_session, project.id)
    items = evidence["artifacts"]["items"]
    assert len(items) == 1
    previews = items[0]["viewable_previews"]
    # Only the owned video resolved; foreign skipped, svg never signed.
    assert previews == [
        {
            "url": "https://signed.example/rollout.mp4",
            "storage_uri": own_video,
            "kind": "video",
            "filename": "rollout.mp4",
        }
    ]
    assert calls == [(own_video, scp._PREVIEW_SIGNED_URL_MINUTES)]

    descriptor = _artifact_descriptor(items[0])
    assert descriptor["kind"] == "video"
    assert descriptor["video"]["url"] == "https://signed.example/rollout.mp4"


def test_snapshot_sanitizer_strips_signed_preview_urls():
    """NEVER-PERSIST invariant: the strategic-session snapshot seam
    (message.context['strategic_session'] via sanitize_strategic_session_snapshot)
    is a field allowlist that structurally drops image/video/preview payloads,
    so a signed URL can never reach a durable row through it."""
    from app.workbench_state import sanitize_strategic_session_snapshot

    leaky_descriptor = {
        "kind": "image",
        "id": "artifact:abc",
        "label": "Loss curve",
        "summary": "training run",
        "status": "ready",
        "target": {"surface": "task", "task_id": "t-1"},
        "image": {"url": "https://signed.example/leak.png"},
        "video": {"url": "https://signed.example/leak.mp4"},
        "preview": {"url": "https://signed.example/leak2.png"},
        "viewable_previews": [
            {
                "url": "https://signed.example/leak3.png",
                "kind": "image",
                "filename": "leak3.png",
            }
        ],
    }
    snapshot = sanitize_strategic_session_snapshot(
        {
            "selected_ref_descriptors": [leaky_descriptor],
            "context_package_descriptor": leaky_descriptor,
            "context_package_id": "strategic-context:p-1",
        }
    )

    def _assert_no_urls(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                assert key not in (
                    "image",
                    "video",
                    "preview",
                    "viewable_previews",
                    "url",
                ), f"signed-url field {key!r} leaked into the persisted snapshot"
                _assert_no_urls(value)
        elif isinstance(obj, list):
            for value in obj:
                _assert_no_urls(value)
        elif isinstance(obj, str):
            assert "signed.example" not in obj

    _assert_no_urls(snapshot)
    # The pointer fields themselves survive (the snapshot stays useful).
    assert snapshot["selected_ref_descriptors"][0]["id"] == "artifact:abc"
    assert snapshot["context_package_descriptor"]["id"] == "artifact:abc"


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
    assert _artifact_renderable_kind({"kind": "recording.mov", "label": "run"}) == "video"


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


def test_artifact_renderable_kind_pdf():
    """Artifact with pdf tokens yields kind='pdf' (matplotlib savefig('*.pdf'))."""
    assert _artifact_renderable_kind({"artifact_type": "COMPUTE_RESULT", "logical_key": "figure.pdf"}) == "pdf"
    assert _artifact_renderable_kind({"kind": "report.pdf", "label": "run"}) == "pdf"
    assert _artifact_renderable_kind({"label": "pdf export"}) == "pdf"


def test_artifact_descriptor_pdf_with_preview_url():
    """PDF artifact with a viewable_preview renders kind='pdf' with a resolvable
    url/storage_uri and drops unsupported_reason."""
    item = {
        "id": "art-pdf-1",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "report.pdf",
        "label": "Report",
        "task_title": "Plotting run",
        "task_status": "completed",
        "viewable_previews": [
            {
                "url": "https://signed.example.com/report.pdf?sig=xyz",
                "kind": "pdf",
                "filename": "report.pdf",
                "storage_uri": "gs://bucket/compute-jobs/j/artifacts/viewable/report.pdf",
            },
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "pdf"
    assert desc["pdf"]["url"] == "https://signed.example.com/report.pdf?sig=xyz"
    assert desc["pdf"]["storage_uri"] == (
        "gs://bucket/compute-jobs/j/artifacts/viewable/report.pdf"
    )
    assert desc["pdfs"][0]["filename"] == "report.pdf"
    assert "unsupported_reason" not in desc


def test_artifact_descriptor_pdf_no_preview():
    """PDF-looking artifact without a signable preview renders the fail-soft stub."""
    item = {
        "id": "art-pdf-2",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "report.pdf",
        "label": "Report",
        "task_title": "Plotting run",
        "task_status": "completed",
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["kind"] == "pdf"
    assert desc["pdf"]["url"] is None
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


def test_artifact_descriptor_preserves_all_mixed_media_preview_groups():
    """Image precedence identifies the tile without dropping video or PDF evidence."""
    item = {
        "id": "art-mixed-media",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "results/",
        "label": "Training results",
        "viewable_previews": [
            {"url": "https://signed.example.com/reward.png", "kind": "image", "filename": "reward.png"},
            {"url": "https://signed.example.com/rollout.mp4", "kind": "video", "filename": "rollout.mp4"},
            {"url": "https://signed.example.com/report.pdf", "kind": "pdf", "filename": "report.pdf"},
        ],
    }

    desc = _artifact_descriptor(item)

    assert desc is not None
    assert desc["kind"] == "image"
    assert desc["images"][0]["filename"] == "reward.png"
    assert desc["videos"][0]["filename"] == "rollout.mp4"
    assert desc["pdfs"][0]["filename"] == "report.pdf"


def test_artifact_descriptor_image_carries_storage_uri_for_lazy_resolution():
    """Image descriptors expose storage_uri so the frontend can re-resolve a
    fresh signed URL when the baked (5-minute) URL expires on an idle canvas."""
    item = {
        "id": "art-lazy-img",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "plot.png",
        "label": "Plot",
        "task_title": "Run",
        "task_status": "completed",
        "viewable_previews": [
            {
                "url": "https://signed.example.com/plot.png?sig=abc",
                "kind": "image",
                "filename": "plot.png",
                "storage_uri": "gs://bucket/plot.png",
            },
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["image"]["storage_uri"] == "gs://bucket/plot.png"
    assert desc["images"][0]["storage_uri"] == "gs://bucket/plot.png"


def test_artifact_descriptor_video_carries_storage_uri_for_lazy_resolution():
    """Video descriptors expose storage_uri for the same lazy re-resolution path."""
    item = {
        "id": "art-lazy-vid",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "demo.mp4",
        "label": "Demo",
        "task_title": "Run",
        "task_status": "completed",
        "viewable_previews": [
            {
                "url": "https://signed.example.com/demo.mp4?sig=def",
                "kind": "video",
                "filename": "demo.mp4",
                "storage_uri": "gs://bucket/demo.mp4",
            },
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["video"]["storage_uri"] == "gs://bucket/demo.mp4"
    assert desc["videos"][0]["storage_uri"] == "gs://bucket/demo.mp4"


def test_artifact_descriptor_image_ref_only_is_not_unsupported():
    """A preview with only a storage_uri (no baked url) is still resolvable, so
    the descriptor must NOT be stubbed with unsupported_reason."""
    item = {
        "id": "art-ref-only",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "plot.png",
        "label": "Plot",
        "task_title": "Run",
        "task_status": "completed",
        "viewable_previews": [
            {"url": None, "kind": "image", "filename": "plot.png", "storage_uri": "gs://bucket/plot.png"},
        ],
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert "unsupported_reason" not in desc
    assert desc["image"]["storage_uri"] == "gs://bucket/plot.png"




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
    # No-leak contract (test_strategic_state.py): the raw gs:// reference must
    # never ride on the item — only the signed, transient URLs do.
    assert "storage_uri" not in item
    assert "viewable_previews" in item
    assert len(item["viewable_previews"]) == 2
    assert item["viewable_previews"][0] == {
        "url": "https://signed-url/plot.png?token=abc",
        "kind": "image",
        "filename": "plot.png",
        "storage_uri": "gs://bucket/obj1",
    }
    assert item["viewable_previews"][1] == {
        "url": "https://signed-url/video.mp4?token=def",
        "kind": "video",
        "filename": "video.mp4",
        "storage_uri": "gs://bucket/obj2",
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
    # No-leak contract: the raw gs:// reference never rides on the item.
    assert "storage_uri" not in item
    assert "viewable_previews" not in item


@pytest.mark.asyncio
async def test_compute_artifact_stamped_with_provenance_and_evidence_tier(
    db_session,
):
    """A compute_experiment artifact is stamped with run provenance + evidence
    tier from its backing compute knowledge doc, and the descriptor surfaces
    both."""
    project, task = await _make_project_task(db_session, name="ProvTest")
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type="COMPUTE_RESULT",
            storage_uri="gs://bucket/result",
            logs=None,
        )
    )
    # Canonical source: the compute doc's forward_quality_verdict carries the
    # earned evidence tier and terminal_diagnostics (execution location).
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            source_kind="compute_experiment",
            title="Compute run",
            summary="A run",
            body="body",
            lifecycle_status="consumed",
            forward_quality_verdict={
                "evidence_tier": "covered",
                "terminal_diagnostics": {
                    "executing_backend": "local-runner",
                    "runner_target_id": "runner-42",
                    "runner_hostname": "gpu-box-01",
                },
            },
        )
    )
    await db_session.commit()

    result = await load_strategic_context_evidence(db_session, project.id)
    items = result["artifacts"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["evidence_tier"] == "covered"
    assert item["executing_backend"] == "local-runner"
    assert item["runner_target_id"] == "runner-42"
    assert item["runner_hostname"] == "gpu-box-01"

    descriptor = _artifact_descriptor(item)
    assert descriptor["evidence_tier"] == "covered"
    detail_by_label = {d["label"]: d["value"] for d in descriptor["details"]}
    assert detail_by_label["Executed on"] == "local-runner"
    assert detail_by_label["Runner"] == "gpu-box-01 (runner-42)"


@pytest.mark.asyncio
async def test_non_compute_artifact_gets_no_provenance_or_tier(db_session):
    """An artifact without a backing compute_experiment doc gains no provenance
    or evidence-tier keys, and its descriptor stays unchanged."""
    project, task = await _make_project_task(db_session, name="NoProvTest")
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type="COMPUTE_RESULT",
            storage_uri="gs://bucket/result",
            logs=None,
        )
    )
    # A non-compute knowledge doc for the same task must not stamp provenance.
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            source_kind="web_research",
            title="Research",
            summary="A doc",
            body="body",
            lifecycle_status="pending",
            forward_quality_verdict={"evidence_tier": "covered"},
        )
    )
    await db_session.commit()

    result = await load_strategic_context_evidence(db_session, project.id)
    items = result["artifacts"]["items"]
    assert len(items) == 1
    item = items[0]
    assert "evidence_tier" not in item
    assert "executing_backend" not in item
    assert "runner_target_id" not in item
    assert "runner_hostname" not in item

    descriptor = _artifact_descriptor(item)
    assert "evidence_tier" not in descriptor
    labels = {d["label"] for d in descriptor["details"]}
    assert "Executed on" not in labels
    assert "Runner" not in labels


def test_artifact_descriptor_runner_falls_back_to_target_id_only():
    """When only the runner id (no hostname) is stamped, the Runner detail uses
    the bare id."""
    item = {
        "id": "art-1",
        "kind": "resource_detail",
        "label": "run",
        "artifact_type": "COMPUTE_RESULT",
        "task_status": "completed",
        "executing_backend": "cloud-gpu",
        "runner_target_id": "runner-9",
        "evidence_tier": "attested",
    }
    desc = _artifact_descriptor(item)
    assert desc["evidence_tier"] == "attested"
    detail_by_label = {d["label"]: d["value"] for d in desc["details"]}
    assert detail_by_label["Executed on"] == "cloud-gpu"
    assert detail_by_label["Runner"] == "runner-9"


def test_artifact_descriptor_stamps_viewable_files_dropped():
    """_artifact_descriptor promotes viewable_files_dropped from the item dict
    when it is a positive integer."""
    item = {
        "id": "art-1",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "demo.mp4",
        "label": "Demo",
        "task_title": "Training run",
        "task_status": "completed",
        "viewable_files_dropped": 3,
    }
    desc = _artifact_descriptor(item)
    assert desc is not None
    assert desc["viewable_files_dropped"] == 3


def test_artifact_descriptor_omits_viewable_files_dropped_when_zero():
    """_artifact_descriptor omits viewable_files_dropped from the descriptor
    when the value is 0, negative, or absent."""
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
    assert "viewable_files_dropped" not in desc

    # Explicit zero also omitted.
    item["viewable_files_dropped"] = 0
    desc = _artifact_descriptor(item)
    assert "viewable_files_dropped" not in desc

    # Negative value also omitted.
    item["viewable_files_dropped"] = -1
    desc = _artifact_descriptor(item)
    assert "viewable_files_dropped" not in desc


@pytest.mark.asyncio
async def test_terminal_artifact_viewable_files_dropped_stamped(db_session):
    """A terminal artifact whose logs contain viewable_files_dropped stamps it
    onto the evidence item, and _artifact_descriptor promotes it."""
    project = Project(name="DroppedTerminal", user_id=TEST_USER_ID)
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
        ],
        "viewable_files_dropped": 3,
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
        mock_verify.return_value = None
        mock_sign.return_value = "https://signed-url/plot.png?token=abc"
        result = await load_strategic_context_evidence(db_session, project.id)

    artifacts = result["artifacts"]["items"]
    assert len(artifacts) == 1
    item = artifacts[0]
    assert item["viewable_files_dropped"] == 3

    descriptor = _artifact_descriptor(item)
    assert descriptor["viewable_files_dropped"] == 3


@pytest.mark.asyncio
async def test_terminal_artifact_omits_viewable_files_dropped_when_zero(db_session):
    """A terminal artifact whose logs lack viewable_files_dropped (or have it 0)
    produces an item and descriptor without the key."""
    project = Project(name="NoDropsTerminal", user_id=TEST_USER_ID)
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

    # No viewable_files_dropped in logs.
    logs = {
        "compute_job_id": "job-1",
        "viewable_files": [],
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
        mock_verify.return_value = None
        mock_sign.return_value = "https://signed-url/plot.png?token=abc"
        result = await load_strategic_context_evidence(db_session, project.id)

    artifacts = result["artifacts"]["items"]
    assert len(artifacts) == 1
    item = artifacts[0]
    assert "viewable_files_dropped" not in item

    descriptor = _artifact_descriptor(item)
    assert "viewable_files_dropped" not in descriptor


@pytest.mark.asyncio
async def test_running_job_viewable_files_dropped_stamped(
    db_session, monkeypatch
):
    """A running compute job with viewable_files_dropped in its polling_metadata
    produces an evidence item carrying the dropped count, and _artifact_descriptor
    promotes it."""
    project, task = await _make_project_task(db_session, name="RunningDropped")

    plot_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/compute-jobs/"
        "33333333-3333-3333-3333-333333333333/artifacts/viewable/reward_curve.png"
    )
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local_process",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={
            "viewable_files": [
                {
                    "filename": "reward_curve.png",
                    "storage_uri": plot_uri,
                    "kind": "image",
                    "content_type": "image/png",
                    "size_bytes": 1024,
                }
            ],
            "viewable_files_dropped": 2,
        },
    )
    db_session.add(job)
    await db_session.commit()

    async def fake_verify(*a):
        return None

    async def fake_sign(*a, **kw):
        return "https://signed.example/reward_curve.png"

    monkeypatch.setattr(scp, "verify_artifact_project_ownership", fake_verify)
    monkeypatch.setattr(scp, "generate_download_signed_url", fake_sign)

    evidence = await load_strategic_context_evidence(db_session, project.id)
    items = evidence["artifacts"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["viewable_files_dropped"] == 2

    descriptor = _artifact_descriptor(item)
    assert descriptor["viewable_files_dropped"] == 2
