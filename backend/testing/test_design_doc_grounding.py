"""Tests for ground_design_doc_systems — durable file-tree grounding of
design-doc systems/capabilities, mirroring the playbook stale-ref sweep."""

from app.playbook_learning import ground_design_doc_systems


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
