"""Tests for ground_design_doc_systems — durable file-tree grounding of
design-doc systems/capabilities, mirroring the playbook stale-ref sweep."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from app.playbook_learning import (
    branch_grounding_due,
    ground_design_doc_systems,
    ground_design_doc_systems_branch_aware,
    mark_branch_grounding_done,
    recover_grounding_downgrades,
    recover_grounding_downgrades_branch_aware,
)
from app.shared_state import runtime_config


def _make_repo(tmp_path, *rel_paths):
    """Create a repo_root under tmp_path containing the given rel_paths."""
    for rel in rel_paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# exists")
    return str(tmp_path)


def test_no_repo_root_is_noop():
    """ground_design_doc_systems is a no-op when repo_root is falsy or missing."""
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone.py"],
        }
    }
    result = ground_design_doc_systems(doc, "")
    assert result["systems/auth"]["status"] == "mature"
    assert "presence_warning" not in result["systems/auth"]

    missing_root = ground_design_doc_systems(doc, "/nonexistent/path/xyz")
    assert missing_root["systems/auth"]["status"] == "mature"


def test_branch_aware_grounding_works_without_local_checkout(monkeypatch):
    """The backend-facing path grounds against the integration branch, not a checkout."""
    monkeypatch.setattr(
        "app.source_ref_validation._file_exists_in_repo_branch",
        AsyncMock(return_value=False),
    )
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone.py"],
        }
    }
    result = asyncio.run(
        ground_design_doc_systems_branch_aware(
            doc,
            repo_root="",
            token="ghs_tok",
            owner="acme",
            repo="widget",
            branch="automain",
            sha="deadbeef",
        )
    )
    entry = result["systems/auth"]
    assert entry["status"] == "missing"
    assert "integration branch" in entry["presence_warning"]
    assert entry["grounding_provenance"]["source"] == "github-snapshot"
    assert entry["grounding_provenance"]["branch"] == "automain"
    assert entry["grounding_provenance"]["sha"] == "deadbeef"
    assert entry["grounding_provenance"]["checked_at"]
    assert result["_meta"]["updated_by"] == "design-doc-grounding"


def test_grounding_cap_breach_persists_no_downgrades_and_emits_diagnostic(
    tmp_path, monkeypatch, caplog
):
    """Cap breaches leave entries untouched and surface a machine-readable diagnostic."""
    repo_root = _make_repo(tmp_path, "backend/app/present.py")
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone_auth.py"],
        },
        "systems/billing": {
            "status": "functional",
            "modules": ["backend/app/gone_billing.py"],
        },
        "capabilities/retries": {
            "status": "working",
            "notes": "See backend/app/gone_retry.py.",
        },
        "capabilities/search": {
            "status": "partial",
            "notes": "See backend/app/gone_search.py.",
        },
    }
    monkeypatch.setitem(
        runtime_config,
        "playbook_design_doc_grounding_max_downgrades",
        2,
    )
    caplog.set_level(logging.WARNING)
    expected_keys = list(doc)

    result = ground_design_doc_systems(doc, repo_root)

    for key in expected_keys:
        assert result[key]["status"] == doc[key]["status"]
        assert "presence_warning" not in result[key]
        assert "grounding_provenance" not in result[key]

    diagnostic = result["_meta"]["grounding_diagnostic"]
    assert diagnostic["kind"] == "cap_breach"
    assert diagnostic["source"] == "local-disk"
    assert diagnostic["candidate_count"] == 4
    assert diagnostic["cap"] == 2
    assert diagnostic["affected_keys"] == expected_keys
    assert any("Grounding cap breach" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_branch_aware_grounding_cap_breach_persists_no_downgrades_and_emits_diagnostic(
    monkeypatch, caplog
):
    """GitHub-snapshot cap breaches leave entries untouched and emit diagnostics."""
    branch_exists = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.source_ref_validation._file_exists_in_repo_branch",
        branch_exists,
    )
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone_auth.py"],
        },
        "systems/billing": {
            "status": "functional",
            "modules": ["backend/app/gone_billing.py"],
        },
        "capabilities/retries": {
            "status": "working",
            "notes": "See backend/app/gone_retry.py.",
        },
        "capabilities/search": {
            "status": "partial",
            "notes": "See backend/app/gone_search.py.",
        },
    }
    monkeypatch.setitem(
        runtime_config,
        "playbook_design_doc_grounding_max_downgrades",
        2,
    )
    caplog.set_level(logging.WARNING)
    expected_keys = list(doc)

    result = await ground_design_doc_systems_branch_aware(
        doc,
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
        sha="deadbeef",
        project_id="p1",
    )

    assert branch_exists.await_count == 4
    for key in expected_keys:
        assert result[key]["status"] == doc[key]["status"]
        assert "presence_warning" not in result[key]
        assert "grounding_provenance" not in result[key]

    diagnostic = result["_meta"]["grounding_diagnostic"]
    assert diagnostic["kind"] == "cap_breach"
    assert diagnostic["source"] == "github-snapshot"
    assert diagnostic["candidate_count"] == 4
    assert diagnostic["cap"] == 2
    assert diagnostic["affected_keys"] == expected_keys
    assert diagnostic["indeterminate"] is True
    assert any(
        "Grounding cap breach on github-snapshot source" in record.message
        for record in caplog.records
    )


def test_system_all_modules_absent_downgraded(tmp_path):
    """A present system whose every module path is absent is downgraded to missing."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone_one.py", "backend/app/gone_two.py"],
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    entry = result["systems/auth"]
    assert entry["status"] == "missing"
    assert "absent" in entry["presence_warning"]
    assert entry["grounding_provenance"]["source"] == "local-disk"
    assert entry["grounding_provenance"]["checked_at"]
    assert result["_meta"]["updated_by"] == "design-doc-grounding"
    assert result["_meta"]["revision"] == 1


def test_system_partial_hit_untouched(tmp_path):
    """A present system with at least one existing module path is left alone."""
    repo_root = _make_repo(tmp_path, "backend/app/present.py")
    doc = {
        "systems/auth": {
            "status": "functional",
            "modules": ["backend/app/present.py", "backend/app/gone.py"],
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["systems/auth"]["status"] == "functional"
    assert "presence_warning" not in result["systems/auth"]
    assert "_meta" not in result


def test_no_path_pointer_untouched(tmp_path):
    """A present entry with no path pointer is left untouched (no evidence of absence)."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/auth": {
            "status": "mature",
            "description": "Handles authentication with no file references.",
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["systems/auth"]["status"] == "mature"
    assert "presence_warning" not in result["systems/auth"]


def test_non_present_status_untouched(tmp_path):
    """Entries already in a non-present status (stub/missing) are skipped."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/stubbed": {
            "status": "stub",
            "modules": ["backend/app/gone.py"],
        },
        "systems/gone": {
            "status": "missing",
            "modules": ["backend/app/gone.py"],
        },
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["systems/stubbed"]["status"] == "stub"
    assert result["systems/gone"]["status"] == "missing"
    assert "presence_warning" not in result["systems/gone"]
    assert "_meta" not in result


def test_capability_absent_notes_path_downgraded(tmp_path):
    """A working capability whose notes path is absent is downgraded to missing."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "capabilities/retries": {
            "status": "working",
            "notes": "Implemented in backend/app/gone_retry.py end to end.",
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    entry = result["capabilities/retries"]
    assert entry["status"] == "missing"
    assert "absent" in entry["presence_warning"]
    assert result["_meta"]["updated_by"] == "design-doc-grounding"


def test_present_with_existing_files_untouched(tmp_path):
    """A present system whose module files all exist is left untouched."""
    repo_root = _make_repo(
        tmp_path, "backend/app/one.py", "backend/app/two.py"
    )
    doc = {
        "systems/core": {
            "status": "mature",
            "modules": ["backend/app/one.py", "backend/app/two.py"],
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["systems/core"]["status"] == "mature"
    assert "presence_warning" not in result["systems/core"]
    assert "_meta" not in result


def test_other_keys_ignored(tmp_path):
    """playbook/* and other non-system/capability keys are ignored."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "playbook/tip": {
            "status": "mature",
            "modules": ["backend/app/gone.py"],
            "content": "See backend/app/gone.py",
        },
        "summary": {"status": "working", "modules": ["backend/app/gone.py"]},
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["playbook/tip"]["status"] == "mature"
    assert "presence_warning" not in result["playbook/tip"]
    assert result["summary"]["status"] == "working"
    assert "_meta" not in result


def _write(tmp_path, rel, content):
    """Create a file under tmp_path with explicit content and return repo_root."""
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return str(tmp_path)


def test_symbol_present_in_present_file_untouched(tmp_path):
    """Present file + present cited symbol → not flagged (no false positive)."""
    repo_root = _write(
        tmp_path, "backend/app/real.py", "def ground_systems():\n    return 1\n"
    )
    doc = {
        "capabilities/grounding": {
            "status": "working",
            "notes": "Implemented in backend/app/real.py:ground_systems end to end.",
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["capabilities/grounding"]["status"] == "working"
    assert "presence_warning" not in result["capabilities/grounding"]
    assert "_meta" not in result


def test_symbol_absent_from_present_file_downgraded(tmp_path):
    """Present file + absent cited symbol → downgraded to missing (the core case)."""
    # The file exists but the cited symbol was deleted/moved (a re-export facade).
    repo_root = _write(
        tmp_path, "backend/app/facade.py", "from app.real import other_thing\n"
    )
    doc = {
        "capabilities/grounding": {
            "status": "working",
            "notes": "The backend/app/facade.py:ground_systems path handles it.",
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    entry = result["capabilities/grounding"]
    assert entry["status"] == "missing"
    assert "ground_systems" in entry["presence_warning"]
    assert "file exists" in entry["presence_warning"]
    assert result["_meta"]["updated_by"] == "design-doc-grounding"


def test_symbol_absent_via_module_pointer_downgraded(tmp_path):
    """A module pointer that names a symbol gone from a present file is flagged."""
    repo_root = _write(
        tmp_path, "backend/app/facade.py", "from app.real import other_thing\n"
    )
    doc = {
        "systems/core": {
            "status": "mature",
            "modules": ["backend/app/facade.py:ground_systems"],
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    entry = result["systems/core"]
    assert entry["status"] == "missing"
    assert "ground_systems" in entry["presence_warning"]


def test_module_pointer_with_present_symbol_untouched(tmp_path):
    """Module pointer naming a present symbol is NOT flagged at file or symbol level."""
    repo_root = _write(
        tmp_path, "backend/app/core.py", "class GroundSystems:\n    pass\n"
    )
    doc = {
        "systems/core": {
            "status": "mature",
            "modules": ["backend/app/core.py:GroundSystems"],
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["systems/core"]["status"] == "mature"
    assert "presence_warning" not in result["systems/core"]
    assert "_meta" not in result


def test_symbol_indeterminate_missing_file_not_symbol_flagged(tmp_path):
    """Symbol ref to an absent file is handled by the file-level check, not flagged
    as a symbol absence (no false 'symbol' verdict when the file itself is gone)."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "capabilities/grounding": {
            "status": "working",
            "notes": "See backend/app/gone.py:vanished for the implementation.",
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    entry = result["capabilities/grounding"]
    # File is absent → file-level grounding fires with the file-level message,
    # not the symbol-level one.
    assert entry["status"] == "missing"
    assert "all referenced module file(s) are absent" in entry["presence_warning"]


def test_grounding_is_idempotent(tmp_path):
    """Re-running the sweep does not re-bump _meta once an entry is downgraded."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/auth": {
            "status": "partial",
            "modules": ["backend/app/gone.py"],
        }
    }
    first = ground_design_doc_systems(doc, repo_root)
    assert first["systems/auth"]["status"] == "missing"
    rev_after_first = first["_meta"]["revision"]

    second = ground_design_doc_systems(first, repo_root)
    # Already "missing" → falls out of the present-status set, no further change.
    assert second["_meta"]["revision"] == rev_after_first


# ---------------------------------------------------------------------------
# Self-healing recovery tests
# ---------------------------------------------------------------------------


def test_downgrade_preserves_grounding_prior_status(tmp_path):
    """Downgrade stores the original status in grounding_prior_status."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone.py"],
        }
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert result["systems/auth"]["grounding_prior_status"] == "mature"


def test_downgrade_appends_reconciled_marker(tmp_path):
    """Downgrade appends a [reconciled: ...] marker to the appropriate text field."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    # systems/* → description field
    doc = {
        "systems/auth": {
            "status": "mature",
            "description": "Auth module.",
            "modules": ["backend/app/gone.py"],
        },
        "capabilities/retries": {
            "status": "working",
            "notes": "See backend/app/gone_retry.py.",
        },
    }
    result = ground_design_doc_systems(doc, repo_root)
    assert "[reconciled:" in result["systems/auth"]["description"]
    assert "Auth module." in result["systems/auth"]["description"]
    assert "[reconciled:" in result["capabilities/retries"]["notes"]


def test_recover_restores_when_path_returns(tmp_path):
    """Round-trip: downgrade then recover restores status and clears fields."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/auth": {
            "status": "mature",
            "description": "Auth subsystem.",
            "modules": ["backend/app/gone.py"],
        }
    }
    doc = ground_design_doc_systems(doc, repo_root)
    assert doc["systems/auth"]["status"] == "missing"

    # Restore the file.
    gone = tmp_path / "backend" / "app" / "gone.py"
    gone.parent.mkdir(parents=True, exist_ok=True)
    gone.write_text("# restored")

    doc = recover_grounding_downgrades(doc, repo_root)
    recovered = doc["systems/auth"]
    assert recovered["status"] == "mature"
    assert "presence_warning" not in recovered
    assert "grounding_prior_status" not in recovered
    assert "[reconciled:" not in recovered.get("description", "")
    assert "Auth subsystem." in recovered.get("description", "")
    assert doc["_meta"]["updated_by"] == "design-doc-grounding-recovery"


def test_recover_no_op_when_still_absent(tmp_path):
    """Recovery is a no-op when all referenced paths are still absent."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone.py"],
        }
    }
    doc = ground_design_doc_systems(doc, repo_root)
    rev_after_downgrade = doc["_meta"]["revision"]

    doc = recover_grounding_downgrades(doc, repo_root)
    assert doc["systems/auth"]["status"] == "missing"
    assert doc["_meta"]["revision"] == rev_after_downgrade


def test_recover_no_op_without_grounding_prior_status(tmp_path):
    """Authored-missing entries (no presence_warning) are never touched by recovery."""
    repo_root = _make_repo(tmp_path, "backend/app/auth.py")
    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/auth.py"],
        }
    }
    doc = recover_grounding_downgrades(doc, repo_root)
    assert doc["systems/auth"]["status"] == "missing"
    assert "_meta" not in doc


def test_recover_branch_aware_restores(monkeypatch):
    """Branch-aware recovery restores when _file_exists_in_repo_branch returns True."""
    monkeypatch.setattr(
        "app.source_ref_validation._file_exists_in_repo_branch",
        AsyncMock(return_value=True),
    )
    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/auth.py"],
            "presence_warning": "claimed mature but all referenced module file(s) are absent on the integration branch — treat as missing",
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
    assert result["_meta"]["updated_by"] == "design-doc-grounding-recovery"


def test_downgrade_then_recover_idempotent(tmp_path):
    """Two consecutive recover calls on a restored entry leave the state unchanged."""
    repo_root = _make_repo(tmp_path, "backend/app/real.py")
    doc = {
        "systems/auth": {
            "status": "mature",
            "modules": ["backend/app/gone.py"],
        }
    }
    doc = ground_design_doc_systems(doc, repo_root)

    gone = tmp_path / "backend" / "app" / "gone.py"
    gone.parent.mkdir(parents=True, exist_ok=True)
    gone.write_text("# restored")

    doc = recover_grounding_downgrades(doc, repo_root)
    rev_after_first_recover = doc["_meta"]["revision"]

    doc = recover_grounding_downgrades(doc, repo_root)
    # Entry now has status=mature (not missing) → recovery skips it.
    assert doc["systems/auth"]["status"] == "mature"
    assert doc["_meta"]["revision"] == rev_after_first_recover


def test_recovery_runs_when_cooldown_not_elapsed(monkeypatch):
    """Recovery fires on every claim regardless of the 6h downgrade cooldown.

    Simulates the path in agent.py where recover_grounding_downgrades_branch_aware
    is called unconditionally before the branch_grounding_due guard.  Even when
    the cooldown has NOT elapsed (branch_grounding_due returns False), recovery
    should restore a falsely-downgraded entry and ground_design_doc_systems_branch_aware
    must NOT be called.
    """
    monkeypatch.setattr(
        "app.source_ref_validation._file_exists_in_repo_branch",
        AsyncMock(return_value=True),
    )

    doc = {
        "systems/auth": {
            "status": "missing",
            "modules": ["backend/app/auth.py"],
            "presence_warning": (
                "claimed mature but all referenced module file(s) are absent on the"
                " integration branch — treat as missing"
            ),
            "grounding_prior_status": "mature",
        }
    }
    # Stamp the cooldown so branch_grounding_due returns False.
    mark_branch_grounding_done(doc)
    assert not branch_grounding_due(doc), "precondition: cooldown window active"

    # Recovery should still fire and restore the entry.
    recovered = asyncio.run(
        recover_grounding_downgrades_branch_aware(
            doc,
            token="ghs_tok",
            owner="acme",
            repo="widget",
            branch="automain",
        )
    )

    assert recovered["systems/auth"]["status"] == "mature"
    assert "presence_warning" not in recovered["systems/auth"]
    assert recovered["_meta"]["updated_by"] == "design-doc-grounding-recovery"

    # The full downgrade sweep (ground_design_doc_systems_branch_aware) must
    # NOT have been called — it stays behind the cooldown guard.
    sweep_called = False

    async def _mock_sweep(d, **kw):
        nonlocal sweep_called
        sweep_called = True
        return d

    monkeypatch.setattr(
        "app.playbook_storage.ground_design_doc_systems_branch_aware",
        _mock_sweep,
    )
    # Confirm: with the cooldown still active, calling only the recovery
    # function leaves sweep_called False.
    asyncio.run(
        recover_grounding_downgrades_branch_aware(
            doc,
            token="ghs_tok",
            owner="acme",
            repo="widget",
            branch="automain",
        )
    )
    assert not sweep_called, "downgrade sweep must not run when cooldown is active"
