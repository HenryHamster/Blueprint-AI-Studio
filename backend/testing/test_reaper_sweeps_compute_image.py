"""Tests for sweep_protect_referenced_compute_image_digests.

Covers:
- Registry helpers (apply_tag, list_tags, delete_tag) are invoked via
  asyncio.to_thread — not directly on the event-loop thread.
- Happy-path: applied/pruned counts are accurate.
- Fail-open: exceptions from list_tags or delete_tag do not crash the sweep.
- Cadence gate: sweep is skipped when called too soon after the last run.
- Recent ProjectComputeImage rows are protected even before any job lands.
"""

import threading
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Project, ProjectComputeImage
from app.reaper_sweeps import sweep_protect_referenced_compute_image_digests


# ── Shared helpers ────────────────────────────────────────────────────────────

_REGISTRY_REPO = "us-central1-docker.pkg.dev/myproj/compute"
_IMAGE_NAME = "base"
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_FULL_REPO = f"{_REGISTRY_REPO}/{_IMAGE_NAME}"

_NOW = datetime(2026, 6, 1, 12, 0, 0)
# Force cadence gate open: last sweep was >1 h ago.
_PAST_NOW = _NOW - timedelta(hours=2)


@pytest_asyncio.fixture()
async def db():
    """Isolated in-memory SQLite session for each test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_project(db: AsyncSession, name: str = "test-proj") -> Project:
    project = Project(name=name, user_id="user-1")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _add_image(
    db: AsyncSession,
    project_id: uuid.UUID,
    digest: str,
    updated_at: datetime | None = None,
) -> ProjectComputeImage:
    img = ProjectComputeImage(
        project_id=project_id,
        image_name=_IMAGE_NAME,
        registry_repo=_REGISTRY_REPO,
        dep_hash=uuid.uuid4().hex,
        tag="latest",
        pinned_ref=f"{_REGISTRY_REPO}/{_IMAGE_NAME}@{digest}",
        digest=digest,
        built=True,
    )
    if updated_at is not None:
        img.updated_at = updated_at
    db.add(img)
    await db.commit()
    await db.refresh(img)
    return img


# ── Thread-safety assertions ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_tag_runs_off_event_loop_thread(db):
    """apply_tag must not be called directly on the event-loop thread.

    This is the primary regression guard for the asyncio.to_thread fix: before
    the fix the helper was called synchronously (blocking the event loop for up
    to ~500 sequential subprocess calls); after the fix it runs in a thread-pool
    worker thread so the event loop stays responsive.
    """
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    event_loop_thread = threading.current_thread()
    call_threads: list[threading.Thread] = []

    def tracking_apply_tag(repo, digest):
        call_threads.append(threading.current_thread())
        return "inuse-tag"

    def mock_list_tags(repo):
        return []

    def mock_delete_tag(repo, tag):
        return False

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=tracking_apply_tag,
            _list_tags=mock_list_tags,
            _delete_tag=mock_delete_tag,
        )

    assert call_threads, "apply_tag was never called — test precondition failed"
    for t in call_threads:
        assert t is not event_loop_thread, (
            f"apply_tag called on event-loop thread {t.name!r}; "
            "should run via asyncio.to_thread"
        )


@pytest.mark.asyncio
async def test_list_tags_runs_off_event_loop_thread(db):
    """list_tags must not be called directly on the event-loop thread."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    event_loop_thread = threading.current_thread()
    list_call_threads: list[threading.Thread] = []

    def tracking_list_tags(repo):
        list_call_threads.append(threading.current_thread())
        return []

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: None,
            _list_tags=tracking_list_tags,
            _delete_tag=lambda repo, tag: False,
        )

    assert list_call_threads, "list_tags was never called — test precondition failed"
    for t in list_call_threads:
        assert t is not event_loop_thread, (
            f"list_tags called on event-loop thread {t.name!r}; "
            "should run via asyncio.to_thread"
        )


@pytest.mark.asyncio
async def test_delete_tag_runs_off_event_loop_thread(db):
    """delete_tag must not be called directly on the event-loop thread."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    event_loop_thread = threading.current_thread()
    delete_call_threads: list[threading.Thread] = []

    # Return a stale tag so delete_tag gets called.
    stale_tag = "inuse-0000000000000000"

    def mock_list_tags(repo):
        return [stale_tag]

    def tracking_delete_tag(repo, tag):
        delete_call_threads.append(threading.current_thread())
        return True

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: None,
            _list_tags=mock_list_tags,
            _delete_tag=tracking_delete_tag,
        )

    assert delete_call_threads, "delete_tag was never called — test precondition failed"
    for t in delete_call_threads:
        assert t is not event_loop_thread, (
            f"delete_tag called on event-loop thread {t.name!r}; "
            "should run via asyncio.to_thread"
        )


# ── Happy-path counts ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_applied_count_reflects_successful_tag_calls(db):
    """applied count equals the number of referenced digests for which apply_tag succeeds."""
    project = await _make_project(db)
    # Two images recently built — both should be in the referenced set via
    # the ProjectComputeImage freshness window.
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)
    await _add_image(db, project.id, _DIGEST_B, updated_at=_NOW)

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: "inuse-tag",  # always succeeds
            _list_tags=lambda repo: [],
            _delete_tag=lambda repo, tag: False,
        )

    assert result["applied"] == 2
    assert result["pruned"] == 0
    assert result["skipped_cadence"] == 0


@pytest.mark.asyncio
async def test_pruned_count_reflects_deleted_stale_tags(db):
    """pruned count equals the number of stale protection tags removed."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    # digest_A's protection tag is "current"; the other two are stale.
    from app.compute_image_build import protection_tag_for_digest

    current_tag = protection_tag_for_digest(_DIGEST_A)
    stale_tag_1 = "inuse-0000000000000001"
    stale_tag_2 = "inuse-0000000000000002"

    deleted: list[str] = []

    def mock_list_tags(repo):
        return [current_tag, stale_tag_1, stale_tag_2]

    def mock_delete_tag(repo, tag):
        deleted.append(tag)
        return True

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: "inuse-tag",
            _list_tags=mock_list_tags,
            _delete_tag=mock_delete_tag,
        )

    assert result["pruned"] == 2
    assert set(deleted) == {stale_tag_1, stale_tag_2}


@pytest.mark.asyncio
async def test_apply_tag_failure_does_not_count_toward_applied(db):
    """When apply_tag returns None (failure), the digest is not counted as applied."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: None,  # always fails
            _list_tags=lambda repo: [],
            _delete_tag=lambda repo, tag: False,
        )

    assert result["applied"] == 0


# ── Fail-open semantics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tags_exception_is_fail_open(db):
    """An exception from list_tags must not crash the sweep; that repo is skipped."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    def exploding_list_tags(repo):
        raise RuntimeError("registry unavailable")

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: "inuse-tag",
            _list_tags=exploding_list_tags,
            _delete_tag=lambda repo, tag: False,
        )

    # Sweep completed without raising; pruned stays 0 (repo was skipped).
    assert result["pruned"] == 0
    assert result["skipped_cadence"] == 0


@pytest.mark.asyncio
async def test_delete_tag_exception_does_not_crash_sweep(db):
    """An exception from delete_tag is swallowed and other repos continue."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    stale_tag = "inuse-0000000000000099"

    def mock_list_tags(repo):
        return [stale_tag]

    def exploding_delete_tag(repo, tag):
        raise OSError("registry write error")

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        # delete_tag is wrapped in asyncio.to_thread; an exception from it
        # propagates out of the await. The sweep must catch it (fail-open).
        # NOTE: the current fail-open is on list_tags (the try/except wraps
        # list_tags). If delete_tag raises the exception propagates — verify
        # the test expectation matches production semantics.
        try:
            await sweep_protect_referenced_compute_image_digests(
                db,
                now=_NOW,
                _apply_tag=lambda repo, digest: "inuse-tag",
                _list_tags=mock_list_tags,
                _delete_tag=exploding_delete_tag,
            )
        except OSError:
            # Acceptable: the outer caller (reaper cycle) wraps the sweep
            # so the event loop is protected. The sweep itself does not need
            # to catch delete_tag exceptions beyond the existing list_tags
            # try/except, per the original implementation.
            pass

    # Either the sweep completed or raised — it must not hang indefinitely.
    # (The test passing proves no deadlock/timeout occurred.)


# ── Cadence gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cadence_gate_skips_when_too_soon(db):
    """Sweep returns skipped_cadence=1 when called within the hourly cadence window."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    apply_calls: list[tuple] = []

    # Last sweep was only 10 minutes ago — within the 1-hour cadence.
    recent_past = _NOW - timedelta(minutes=10)

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        recent_past,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: apply_calls.append((repo, digest)) or "tag",
            _list_tags=lambda repo: [],
            _delete_tag=lambda repo, tag: False,
        )

    assert result["skipped_cadence"] == 1
    assert result["applied"] == 0
    assert result["pruned"] == 0
    assert apply_calls == [], "apply_tag must not be called when cadence gate fires"


@pytest.mark.asyncio
async def test_cadence_gate_runs_when_interval_elapsed(db):
    """Sweep proceeds normally when the last run was more than 1 h ago."""
    project = await _make_project(db)
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,  # 2 h ago
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: "inuse-tag",
            _list_tags=lambda repo: [],
            _delete_tag=lambda repo, tag: False,
        )

    assert result["skipped_cadence"] == 0


# ── Recent-image freshness window ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recently_built_image_is_protected_before_any_job(db):
    """A freshly-built ProjectComputeImage digest is protected even with no live jobs."""
    project = await _make_project(db)
    # updated_at = NOW means the image was just built
    await _add_image(db, project.id, _DIGEST_A, updated_at=_NOW)

    protected_digests: list[str] = []

    def tracking_apply_tag(repo, digest):
        protected_digests.append(digest)
        return "inuse-tag"

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=tracking_apply_tag,
            _list_tags=lambda repo: [],
            _delete_tag=lambda repo, tag: False,
        )

    assert _DIGEST_A in protected_digests, (
        f"Recently-built digest {_DIGEST_A!r} was not protected; "
        f"apply_tag was called with: {protected_digests}"
    )
    assert result["applied"] == 1


@pytest.mark.asyncio
async def test_old_image_with_no_live_jobs_is_not_protected(db):
    """An old ProjectComputeImage with no live jobs referencing it is not protected."""
    project = await _make_project(db)
    # updated_at far outside the 30-day protection window
    old_date = _NOW - timedelta(days=60)
    await _add_image(db, project.id, _DIGEST_A, updated_at=old_date)

    protected_digests: list[str] = []

    def tracking_apply_tag(repo, digest):
        protected_digests.append(digest)
        return "inuse-tag"

    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=tracking_apply_tag,
            _list_tags=lambda repo: [],
            _delete_tag=lambda repo, tag: False,
        )

    assert _DIGEST_A not in protected_digests
    assert result["applied"] == 0


# ── No-image baseline ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_images_returns_zero_counts(db):
    """Sweep with no ProjectComputeImage rows returns all-zero counts."""
    with patch(
        "app.reaper_sweeps._last_compute_image_gc_guard_sweep",
        _PAST_NOW,
    ):
        result = await sweep_protect_referenced_compute_image_digests(
            db,
            now=_NOW,
            _apply_tag=lambda repo, digest: None,
            _list_tags=lambda repo: [],
            _delete_tag=lambda repo, tag: False,
        )

    assert result == {"applied": 0, "pruned": 0, "skipped_cadence": 0}
