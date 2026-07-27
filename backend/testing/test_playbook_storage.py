import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.models import Task, TaskStatus, _utcnow
from app.playbook_storage import (
    ground_design_doc_systems,
    recover_grounding_downgrades,
    recover_grounding_downgrades_branch_aware,
)
from testing._playbook_test_helpers import _make_project


def test_archive_playbook_entry_moves_entry_and_trims_archive(monkeypatch):
    from app import playbook_storage as storage
    from app.shared_state import runtime_config

    now_iso = _utcnow().isoformat()
    doc = {
        "playbook_archive": [
            {"_archived_key": "playbook/old-1", "_archived_at": now_iso},
            {"_archived_key": "playbook/old-2", "_archived_at": now_iso},
        ],
        "playbook/tip": {
            "content": "tip",
            "confirmed_count": 1,
            "updated_at": now_iso,
        },
    }

    prev_max = runtime_config.get("playbook_archive_max")
    runtime_config["playbook_archive_max"] = 2
    try:
        archived = storage.archive_playbook_entry(
            doc,
            "playbook/tip",
            archived_at=now_iso,
            retirement_reason="manual_archive",
        )
    finally:
        if prev_max is None:
            runtime_config.pop("playbook_archive_max", None)
        else:
            runtime_config["playbook_archive_max"] = prev_max

    assert archived is True
    assert "playbook/tip" not in doc
    archive = doc["playbook_archive"]
    assert len(archive) == 2
    assert archive[-1]["_archived_key"] == "playbook/tip"
    assert archive[-1]["_retirement_reason"] == "manual_archive"
    assert archive[0]["_archived_key"] == "playbook/old-2"


def test_record_playbook_tombstone_adds_deleted_key_once():
    from app.playbook_storage import record_playbook_tombstone

    now_iso = _utcnow().isoformat()
    doc = {}

    record_playbook_tombstone(doc, "playbook/tip", archived_at=now_iso)
    record_playbook_tombstone(doc, "playbook/tip", archived_at=now_iso)

    deleted_keys = doc["playbook_deleted_keys"]
    assert deleted_keys == ["playbook/tip"]
    archive = doc["playbook_archive"]
    # Idempotent: repeated tombstone for same key produces exactly one archive row
    assert len(archive) == 1
    assert archive[0]["_archived_key"] == "playbook/tip"
    assert archive[0]["_retirement_reason"] == "operator_deleted"


def test_record_playbook_tombstone_idempotent_keeps_ordinary_archive_entries():
    """Ordinary archive entries for the same key are preserved; only operator_deleted is deduped."""
    from app.playbook_storage import _append_archived_playbook_entry, record_playbook_tombstone

    now_iso = _utcnow().isoformat()
    doc: dict = {}

    # Add an ordinary (non-operator_deleted) archive entry for the same key first
    _append_archived_playbook_entry(
        doc, "playbook/tip", {"_retirement_reason": "capacity"}, archived_at=now_iso
    )

    # Two tombstone calls — should produce exactly one operator_deleted row
    record_playbook_tombstone(doc, "playbook/tip", archived_at=now_iso)
    record_playbook_tombstone(doc, "playbook/tip", archived_at=now_iso)

    deleted_keys = doc["playbook_deleted_keys"]
    assert deleted_keys == ["playbook/tip"]

    archive = doc["playbook_archive"]
    # Ordinary entry preserved + exactly one operator_deleted row
    assert len(archive) == 2
    reasons = [e["_retirement_reason"] for e in archive]
    assert "capacity" in reasons
    assert reasons.count("operator_deleted") == 1


def test_playbook_learning_reexports_storage_helpers():
    import app.playbook_learning as learning
    import app.playbook_storage as storage

    assert learning.maybe_archive_for_capacity is storage.maybe_archive_for_capacity
    assert learning.finalize_pending_playbook_entries is (
        storage.finalize_pending_playbook_entries
    )
    assert learning.retire_idle_auto_entries is storage.retire_idle_auto_entries
    # Full lifecycle re-export set must point at the canonical storage objects
    # by identity — guards against a facade shadow copy of any of these.
    assert learning.retire_ineffective_patterns is storage.retire_ineffective_patterns
    assert learning.record_playbook_applied is storage.record_playbook_applied
    assert learning.record_playbook_confirmed is storage.record_playbook_confirmed
    assert learning.check_and_flag_stale_refs is storage.check_and_flag_stale_refs
    assert learning._is_auto_playbook_suppressed is (
        storage._is_auto_playbook_suppressed
    )
    assert learning._get_prior_archive_entry is storage._get_prior_archive_entry


# ─────────────────────────────────────────────────────────────────────────────
# Single-source-of-truth capacity eviction selection
#
# ``_archive_least_useful_entry`` previously had two diverged copies — one on
# the capacity path (playbook_capacity) and one on the storage path
# (playbook_storage) — that disagreed on the effectiveness term for
# rate-observed entries (``1.0 - rate`` vs ``-rate``). The storage copy ranked
# a *proven low-rate* entry as MORE useful than an *untried* one, so it would
# evict the untried entry and keep the proven poor performer — the opposite of
# the documented intent. The two are now unified on the ``1.0 - rate`` formula
# and share one implementation. These tests pin that consolidated selection.
# ─────────────────────────────────────────────────────────────────────────────


def test_archive_least_useful_entry_single_source_of_truth():
    """Both the storage path and the capacity path call the same function object."""
    from app import playbook_capacity as capacity
    from app import playbook_storage as storage

    assert (
        capacity._archive_least_useful_entry is storage._archive_least_useful_entry
    ), "capacity and storage must share one eviction-selection implementation"


def test_storage_eviction_evicts_proven_low_rate_over_untried():
    """A proven low-rate entry is evicted before an untried zero-count entry.

    BEHAVIOR-CHANGING regression pin: the *old* storage-path effectiveness
    (``-confirmed/followed`` ∈ [-1, 0]) made the rate-observed entry rank
    HIGHER (more useful) than the untried entry (``-confirmed`` == 0), so the
    old code would have evicted ``playbook/untried`` and kept the poor
    performer. The consolidated logic (``1.0 - rate`` ∈ [0, 1]) correctly
    evicts the proven poor performer first.
    """
    from app.playbook_storage import _archive_least_useful_entry

    now_iso = _utcnow().isoformat()
    doc = {
        # Proven poor performer: tried often, rarely confirmed (rate = 2/50 = 0.04).
        "playbook/low-performer": {
            "content": "rarely useful",
            "confirmed_count": 2,
            "followed_count": 50,
            "updated_at": now_iso,
        },
        # Never tried: no evidence either way.
        "playbook/untried": {
            "content": "never tried",
            "confirmed_count": 0,
            "followed_count": 0,
            "updated_at": now_iso,
        },
    }

    result = _archive_least_useful_entry(doc)

    # Consolidated semantics: the measured poor performer is archived first.
    assert "playbook/low-performer" not in result, (
        "proven low-rate entry must be evicted before an untried one"
    )
    assert "playbook/untried" in result, (
        "untried entry must be retained over a proven poor performer"
    )
    archive = result.get("playbook_archive", [])
    assert len(archive) == 1
    assert archive[0]["_archived_key"] == "playbook/low-performer"


def test_storage_eviction_prefers_stale_then_rate_then_age():
    """Eviction order: stale first, then worst effectiveness, then oldest."""
    from datetime import timedelta

    from app.playbook_storage import _archive_least_useful_entry

    now = _utcnow()
    doc = {
        # Stale beats everything regardless of strong stats.
        "playbook/stale-strong": {
            "content": "stale",
            "confirmed_count": 99,
            "followed_count": 100,
            "stale_file_refs": True,
            "updated_at": now.isoformat(),
        },
        "playbook/healthy": {
            "content": "healthy",
            "confirmed_count": 40,
            "followed_count": 50,
            "updated_at": now.isoformat(),
        },
    }

    result = _archive_least_useful_entry(doc)
    assert "playbook/stale-strong" not in result, "stale entry must be evicted first"
    assert "playbook/healthy" in result

    # Among equally-effective non-stale entries, the oldest is evicted.
    doc2 = {
        "playbook/old": {
            "content": "old",
            "confirmed_count": 1,
            "followed_count": 10,
            "updated_at": (now - timedelta(days=90)).isoformat(),
        },
        "playbook/recent": {
            "content": "recent",
            "confirmed_count": 1,
            "followed_count": 10,
            "updated_at": now.isoformat(),
        },
    }
    result2 = _archive_least_useful_entry(doc2)
    assert "playbook/old" not in result2, "oldest entry should be evicted within a tier"
    assert "playbook/recent" in result2


def test_is_auto_playbook_suppressed_respects_recent_tombstone():
    from app.playbook_storage import (
        _is_auto_playbook_suppressed,
        record_playbook_tombstone,
    )

    doc = {}
    record_playbook_tombstone(doc, "playbook/tip", archived_at=_utcnow().isoformat())

    assert _is_auto_playbook_suppressed(doc, "playbook/tip") is True
    assert (
        _is_auto_playbook_suppressed(
            {
                "playbook_archive": [
                    {
                        "_archived_key": "playbook/other",
                        "_archived_at": (_utcnow() - timedelta(days=90)).isoformat(),
                    }
                ]
            },
            "playbook/tip",
        )
        is False
    )


# ─────────────────────────────────────────────────────────────────────────────
# Anti-poisoning: finalize_pending_playbook_entries
# ─────────────────────────────────────────────────────────────────────────────


def test_visible_playbook_entry_keeps_prior_truth_during_staged_update():
    from app.playbook_storage import visible_playbook_entry

    prior = {"content": "Committed truth", "source_task_id": "old-task"}
    staged = {
        "content": "Pending replacement",
        "source_task_id": "new-task",
        "pending_source": "new-task",
        "_pending_previous": prior,
    }

    assert visible_playbook_entry(staged, viewer_task_id="new-task") == {
        "content": "Pending replacement",
        "source_task_id": "new-task",
        "pending_source": "new-task",
    }
    assert visible_playbook_entry(staged, viewer_task_id="peer-task") == prior
    assert visible_playbook_entry(
        {
            "content": "Brand-new pending claim",
            "pending_source": "new-task",
        },
        viewer_task_id="peer-task",
    ) is None


def test_mutate_visible_playbook_entry_credits_prior_entry_for_peer():
    from app.playbook_storage import mutate_visible_playbook_entry

    staged = {
        "content": "Pending replacement",
        "pending_source": "writer-task",
        "_pending_previous": {
            "content": "Committed truth",
            "reference_count": 1,
            "confirmed_count": 2,
        },
    }

    def credit(entry):
        entry["reference_count"] = entry.get("reference_count", 0) + 1
        entry["confirmed_count"] = entry.get("confirmed_count", 0) + 1

    updated = mutate_visible_playbook_entry(
        staged,
        viewer_task_id="peer-task",
        mutate=credit,
    )

    assert updated is not None
    assert updated["content"] == "Pending replacement"
    assert "reference_count" not in updated
    assert updated["_pending_previous"]["content"] == "Committed truth"
    assert updated["_pending_previous"]["reference_count"] == 2
    assert updated["_pending_previous"]["confirmed_count"] == 3


@pytest.mark.asyncio
async def test_finalize_pending_playbook_entries_drops_on_failure(db_session):
    """A FAILED task's pending entries are removed so peers never see the lie."""
    from app.playbook_learning import finalize_pending_playbook_entries
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.flush()

    project.design_doc = {
        "playbook/lie": {
            "content": "X exists in foo.py",
            "source_task_id": str(task.id),
            "pending_source": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
        },
        "playbook/peer": {  # written by a different task — must NOT be touched
            "content": "Real, finalised entry",
            "source_task_id": str(uuid.uuid4()),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 1,
        },
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    n = await finalize_pending_playbook_entries(task, db_session, succeeded=False)
    await db_session.flush()

    assert n == 1
    doc = project.design_doc
    assert "playbook/lie" not in doc, "Failed task's pending entry must be removed"
    assert "playbook/peer" in doc, "Other tasks' entries must be preserved"


@pytest.mark.asyncio
async def test_finalize_pending_playbook_entries_promotes_on_success(db_session):
    """A COMPLETED task's pending entries lose pending_source and become visible."""
    from app.playbook_learning import finalize_pending_playbook_entries
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.flush()

    project.design_doc = {
        "playbook/proven": {
            "content": "Real finding from completed task",
            "source_task_id": str(task.id),
            "pending_source": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
        },
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    n = await finalize_pending_playbook_entries(task, db_session, succeeded=True)
    await db_session.flush()

    assert n == 1
    entry = project.design_doc["playbook/proven"]
    assert "pending_source" not in entry, (
        "Successful task's pending flag must be cleared"
    )
    assert entry["content"] == "Real finding from completed task"


@pytest.mark.asyncio
async def test_finalize_failed_pending_update_restores_prior_entry(db_session):
    """A failed staged update must not erase the last committed shared truth."""
    from app.playbook_learning import finalize_pending_playbook_entries
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "x"}],
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.flush()

    prior = {
        "content": "Last committed fact",
        "source_task_id": str(uuid.uuid4()),
        "updated_at": _utcnow().isoformat(),
        "confirmed_count": 2,
    }
    project.design_doc = {
        "playbook/staged-update": {
            "content": "Unshipped replacement",
            "source_task_id": str(task.id),
            "pending_source": str(task.id),
            "_pending_previous": prior,
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 2,
        },
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    n = await finalize_pending_playbook_entries(task, db_session, succeeded=False)
    await db_session.flush()

    assert n == 1
    assert project.design_doc["playbook/staged-update"] == prior


def test_grounding_sweep_restores_falsely_downgraded_entry(tmp_path):
    """Self-healing recovery: downgrade then restore when the path returns.

    Simulates a false downgrade (path temporarily absent from checkout) followed
    by the path being restored. recover_grounding_downgrades must reinstate the
    prior status, clear presence_warning and grounding_prior_status, and strip
    the [reconciled: ...] marker from the text field.
    """
    # Phase 1: path absent → downgrade.
    repo_root = str(tmp_path)
    doc = {
        "systems/auth": {
            "status": "mature",
            "description": "Auth subsystem.",
            "modules": ["backend/app/auth.py"],
        }
    }
    doc = ground_design_doc_systems(doc, repo_root)
    entry = doc["systems/auth"]
    assert entry["status"] == "missing", "Sanity: path absent triggers downgrade"
    assert entry.get("grounding_prior_status") == "mature"
    assert "presence_warning" in entry
    assert "[reconciled:" in entry.get("description", "")

    # Phase 2: path returns → recovery restores.
    auth_path = tmp_path / "backend" / "app" / "auth.py"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text("# restored")

    doc = recover_grounding_downgrades(doc, repo_root)
    recovered = doc["systems/auth"]
    assert recovered["status"] == "mature", "Recovery must reinstate original status"
    assert "presence_warning" not in recovered, "presence_warning must be cleared"
    assert "grounding_prior_status" not in recovered, "grounding_prior_status must be cleared"
    assert "[reconciled:" not in recovered.get("description", ""), "Marker must be stripped"
    assert "Auth subsystem." in recovered.get("description", ""), "Original text must survive"
    assert doc["_meta"]["updated_by"] == "design-doc-grounding-recovery"


def test_grounding_ignores_glob_only_modules(tmp_path):
    """Literal glob pointers do not count as confirmed absence in local mode."""
    repo_root = str(tmp_path)
    repo_file = tmp_path / "backend" / "app" / "routers" / "admin.py"
    repo_file.parent.mkdir(parents=True, exist_ok=True)
    repo_file.write_text("# exists")
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/routers/*.py"],
        }
    }

    result = ground_design_doc_systems(doc, repo_root)
    assert result["systems/auth"]["status"] == "mature"
    assert "presence_warning" not in result["systems/auth"]
    assert "_meta" not in result


def test_grounding_recovery_no_op_when_path_still_absent(tmp_path):
    """Recovery is a no-op when the referenced path is still absent on disk."""
    repo_root = str(tmp_path)
    doc = {
        "systems/billing": {
            "status": "functional",
            "modules": ["backend/app/billing.py"],
        }
    }
    doc = ground_design_doc_systems(doc, repo_root)
    rev_after_downgrade = doc["_meta"]["revision"]

    doc = recover_grounding_downgrades(doc, repo_root)
    # Path still absent → no recovery, revision unchanged.
    assert doc["systems/billing"]["status"] == "missing"
    assert doc["_meta"]["revision"] == rev_after_downgrade


def test_grounding_recovery_symbol_present_restores(tmp_path):
    """Symbol-level recovery restores when the cited symbol is confirmed."""
    repo_root = str(tmp_path)
    symbol_path = tmp_path / "backend" / "app" / "facade.py"
    symbol_path.parent.mkdir(parents=True, exist_ok=True)
    symbol_path.write_text("def restored_symbol():\n    return 1\n")
    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/facade.py:restored_symbol"],
            "presence_warning": "claimed mature but cited symbol 'backend/app/facade.py:restored_symbol' is absent on the integration branch though its file exists — treat as missing",
            "grounding_prior_status": "mature",
        }
    }

    recovered = recover_grounding_downgrades(doc, repo_root)
    entry = recovered["systems/auth"]
    assert entry["status"] == "mature"
    assert "presence_warning" not in entry
    assert "grounding_prior_status" not in entry


def test_grounding_recovery_symbol_indeterminate_leaves_untouched(
    tmp_path, mocker
):
    """Unreadable symbol reads stay indeterminate and must not restore entries."""
    repo_root = str(tmp_path)
    symbol_path = tmp_path / "backend" / "app" / "facade.py"
    symbol_path.parent.mkdir(parents=True, exist_ok=True)
    symbol_path.write_text("def restored_symbol():\n    return 1\n")
    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/facade.py:restored_symbol"],
            "presence_warning": "claimed mature but cited symbol 'backend/app/facade.py:restored_symbol' is absent on the integration branch though its file exists — treat as missing",
            "grounding_prior_status": "mature",
        }
    }

    mocker.patch("builtins.open", side_effect=OSError("denied"))
    recovered = recover_grounding_downgrades(doc, repo_root)
    assert recovered["systems/auth"]["status"] == "missing"
    assert "presence_warning" in recovered["systems/auth"]


def test_grounding_recovery_no_op_for_authored_missing(tmp_path):
    """Authored-missing entries (no presence_warning) are never touched by recovery."""
    repo_root = str(tmp_path)
    billing_path = tmp_path / "backend" / "app" / "billing.py"
    billing_path.parent.mkdir(parents=True, exist_ok=True)
    billing_path.write_text("# exists")
    doc = {
        "systems/billing": {
            "status": "missing",
            "modules": ["backend/app/billing.py"],
        }
    }
    # File present but entry is authored-missing (no presence_warning).
    doc = recover_grounding_downgrades(doc, repo_root)
    assert doc["systems/billing"]["status"] == "missing", "Must not touch authored-missing"
    assert "_meta" not in doc


def test_grounding_recovery_branch_aware_restores(monkeypatch):
    """Branch-aware recovery restores when _file_exists_in_repo_branch returns True."""
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.source_ref_validation._file_exists_in_repo_branch",
        AsyncMock(return_value=True),
    )
    doc = {
        "systems/payments": {
            "status": "missing",
            "modules": ["backend/app/payments.py"],
            "presence_warning": "claimed functional but all referenced module file(s) are absent on the integration branch — treat as missing",
            "grounding_prior_status": "functional",
        }
    }
    result = asyncio.run(
        recover_grounding_downgrades_branch_aware(
            doc,
            token="ghs_tok",
            owner="acme",
            repo="widget",
            branch="automain",
        )
    )
    recovered = result["systems/payments"]
    assert recovered["status"] == "functional"
    assert "presence_warning" not in recovered
    assert result["_meta"]["updated_by"] == "design-doc-grounding-recovery"


def test_grounding_recovery_branch_aware_indeterminate_leaves_untouched(monkeypatch):
    """Recovery is a no-op when _file_exists_in_repo_branch returns None (indeterminate)."""
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.source_ref_validation._file_exists_in_repo_branch",
        AsyncMock(return_value=None),
    )
    doc = {
        "systems/payments": {
            "status": "missing",
            "modules": ["backend/app/payments.py"],
            "presence_warning": "claimed functional but all referenced module file(s) are absent on the integration branch — treat as missing",
            "grounding_prior_status": "functional",
        }
    }
    result = asyncio.run(
        recover_grounding_downgrades_branch_aware(
            doc,
            token="ghs_tok",
            owner="acme",
            repo="widget",
            branch="automain",
        )
    )
    assert result["systems/payments"]["status"] == "missing", "Indeterminate must leave untouched"
    assert "_meta" not in result


def test_grounding_recovery_branch_aware_symbol_restores(mocker):
    """Branch-aware symbol recovery restores only on confirmed symbol presence."""
    import asyncio

    mocker.patch(
        "app.playbook_storage._present_symbol_in_repo_branch",
        new=AsyncMock(return_value=True),
    )
    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/facade.py:restored_symbol"],
            "presence_warning": "claimed mature but cited symbol 'backend/app/facade.py:restored_symbol' is absent on the integration branch though its file exists — treat as missing",
            "grounding_prior_status": "mature",
        }
    }
    result = asyncio.run(
        recover_grounding_downgrades_branch_aware(
            doc,
            token="ghs_tok",
            owner="acme",
            repo="widget",
            branch="automain",
        )
    )
    assert result["systems/auth"]["status"] == "mature"
    assert "presence_warning" not in result["systems/auth"]


def test_grounding_recovery_branch_aware_symbol_indeterminate_leaves_untouched(
    mocker,
):
    """Indeterminate branch symbol reads must not be misread as recovery."""
    import asyncio

    mocker.patch(
        "app.playbook_storage._present_symbol_in_repo_branch",
        new=AsyncMock(return_value=None),
    )
    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/facade.py:restored_symbol"],
            "presence_warning": "claimed mature but cited symbol 'backend/app/facade.py:restored_symbol' is absent on the integration branch though its file exists — treat as missing",
            "grounding_prior_status": "mature",
        }
    }
    result = asyncio.run(
        recover_grounding_downgrades_branch_aware(
            doc,
            token="ghs_tok",
            owner="acme",
            repo="widget",
            branch="automain",
        )
    )
    assert result["systems/auth"]["status"] == "missing"
    assert "presence_warning" in result["systems/auth"]


def test_grounding_recovery_legacy_warning_text(tmp_path):
    """Legacy entries (no grounding_prior_status) are recovered by parsing the warning text."""
    repo_root = str(tmp_path)
    auth_path = tmp_path / "backend" / "app" / "auth.py"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text("# restored")
    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/auth.py"],
            "presence_warning": "claimed mature but all referenced module file(s) are absent on the integration branch — treat as missing",
        }
    }
    doc = recover_grounding_downgrades(doc, repo_root)
    assert doc["systems/auth"]["status"] == "mature", "Legacy warning text must yield prior status"
