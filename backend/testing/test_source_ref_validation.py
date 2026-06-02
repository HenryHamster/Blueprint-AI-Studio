"""Unit tests for the shared file-existence checkers in source_ref_validation.

Covers the generalized helpers extracted so design-doc system verification and
playbook stale-ref flagging share one partition path:
- ``partition_missing`` — pure sync partitioner over an arbitrary predicate.
- ``classify_existing_paths`` — async git-HEAD-backed classifier.
"""

import pytest

from app.source_ref_validation import classify_existing_paths, partition_missing


# ── partition_missing (pure) ──────────────────────────────────────────────────


def test_partition_missing_all_present():
    present, missing = partition_missing(
        ["a.py", "b.py"], lambda p: True
    )
    assert present == ["a.py", "b.py"]
    assert missing == []


def test_partition_missing_all_absent():
    present, missing = partition_missing(
        ["a.py", "b.py"], lambda p: False
    )
    assert present == []
    assert missing == ["a.py", "b.py"]


def test_partition_missing_mixed_preserves_order():
    existing = {"a.py", "c.py"}
    present, missing = partition_missing(
        ["a.py", "b.py", "c.py", "d.py"], lambda p: p in existing
    )
    assert present == ["a.py", "c.py"]
    assert missing == ["b.py", "d.py"]


def test_partition_missing_non_string_treated_as_missing():
    present, missing = partition_missing(
        ["a.py", None, 42], lambda p: True
    )
    assert present == ["a.py"]
    assert missing == [None, 42]


# ── classify_existing_paths (async, git-backed via _file_exists_at_head) ──────


@pytest.mark.asyncio
async def test_classify_existing_paths_all_present(mocker):
    async def _exists(p):
        return True

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["x.py", "y.py"])
    assert present == ["x.py", "y.py"]
    assert missing == []


@pytest.mark.asyncio
async def test_classify_existing_paths_all_missing(mocker):
    async def _exists(p):
        return False

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["x.py", "y.py"])
    assert present == []
    assert missing == ["x.py", "y.py"]


@pytest.mark.asyncio
async def test_classify_existing_paths_mixed(mocker):
    existing = {"keep.py"}

    async def _exists(p):
        return p in existing

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["keep.py", "gone.py"])
    assert present == ["keep.py"]
    assert missing == ["gone.py"]


@pytest.mark.asyncio
async def test_classify_existing_paths_non_string_missing(mocker):
    async def _exists(p):
        return True

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["ok.py", None])
    assert present == ["ok.py"]
    assert missing == [None]


@pytest.mark.asyncio
async def test_classify_existing_paths_uses_repo_branch_context_when_provided(mocker):
    async def _exists(p, **kwargs):
        assert kwargs == {
            "token": "ghs_test",
            "owner": "acme",
            "repo": "widget",
            "branch": "automain",
            "project_id": "proj-123",
        }
        return p == "x.py"

    local_exists = mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        side_effect=AssertionError("local HEAD fallback should not be used"),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch", side_effect=_exists
    )
    present, missing = await classify_existing_paths(
        ["x.py", "y.py"],
        token="ghs_test",
        owner="acme",
        repo="widget",
        branch="automain",
        project_id="proj-123",
    )
    assert present == ["x.py"]
    assert missing == ["y.py"]
    local_exists.assert_not_called()
