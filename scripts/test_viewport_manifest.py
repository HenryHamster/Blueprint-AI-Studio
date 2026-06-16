"""Tests for viewport_manifest.py build() and _run_diff()."""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import viewport_manifest


def _write_png(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Minimal valid PNG header bytes (different content per call = changed status)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)


def _write_png_identical(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)


def _make_dirs(tmp: str) -> tuple[str, str, str]:
    before = os.path.join(tmp, "before")
    after = os.path.join(tmp, "after")
    out = os.path.join(tmp, "manifest.json")
    return before, after, out


def _load(out: str) -> dict:
    with open(out) as fh:
        return json.load(fh)


def test_build_changed_pair_populates_change_pct_and_diff():
    """When a pair is changed and _run_diff succeeds, change_pct and diff are set."""
    with tempfile.TemporaryDirectory() as tmp:
        before, after, out = _make_dirs(tmp)
        rel = os.path.join("mytest-viewport-test-tsx", "mytest-375px.png")
        _write_png(os.path.join(before, rel))
        # Write different bytes so filecmp reports 'changed'
        os.makedirs(os.path.dirname(os.path.join(after, rel)), exist_ok=True)
        with open(os.path.join(after, rel), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"\xff" * 8)

        diff_out_path = os.path.join(tmp, "diff", rel)

        def fake_run_diff(b, a, d):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            with open(d, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n")
            return {"change_pct": 12.5}

        with patch.object(viewport_manifest, "_run_diff", side_effect=fake_run_diff):
            viewport_manifest.build(before, after, "main", "run-1", out)

        manifest = _load(out)
        pairs = manifest["pairs"]
        assert len(pairs) == 1
        p = pairs[0]
        assert p["status"] == "changed"
        assert p["change_pct"] == 12.5
        assert p["diff"] is not None


def test_build_unchanged_pair_leaves_change_pct_none():
    """Unchanged pairs must have change_pct=None and diff=None."""
    with tempfile.TemporaryDirectory() as tmp:
        before, after, out = _make_dirs(tmp)
        rel = os.path.join("mytest-viewport-test-tsx", "mytest-375px.png")
        _write_png_identical(os.path.join(before, rel))
        _write_png_identical(os.path.join(after, rel))

        with patch.object(viewport_manifest, "_run_diff") as mock_diff:
            viewport_manifest.build(before, after, "main", "run-2", out)
            mock_diff.assert_not_called()

        manifest = _load(out)
        p = manifest["pairs"][0]
        assert p["status"] == "unchanged"
        assert p["change_pct"] is None
        assert p["diff"] is None


def test_build_new_pair_leaves_change_pct_none():
    """New pairs (only in after) must have change_pct=None and diff=None."""
    with tempfile.TemporaryDirectory() as tmp:
        before, after, out = _make_dirs(tmp)
        rel = os.path.join("newtest-viewport-test-tsx", "newtest-375px.png")
        _write_png(os.path.join(after, rel))

        with patch.object(viewport_manifest, "_run_diff") as mock_diff:
            viewport_manifest.build(before, after, "main", "run-3", out)
            mock_diff.assert_not_called()

        manifest = _load(out)
        p = manifest["pairs"][0]
        assert p["status"] == "new"
        assert p["change_pct"] is None
        assert p["diff"] is None


def test_build_removed_pair_leaves_change_pct_none():
    """Removed pairs (only in before) must have change_pct=None and diff=None."""
    with tempfile.TemporaryDirectory() as tmp:
        before, after, out = _make_dirs(tmp)
        rel = os.path.join("oldtest-viewport-test-tsx", "oldtest-375px.png")
        _write_png(os.path.join(before, rel))

        with patch.object(viewport_manifest, "_run_diff") as mock_diff:
            viewport_manifest.build(before, after, "main", "run-4", out)
            mock_diff.assert_not_called()

        manifest = _load(out)
        p = manifest["pairs"][0]
        assert p["status"] == "removed"
        assert p["change_pct"] is None
        assert p["diff"] is None


def test_build_changed_pair_run_diff_failure_leaves_change_pct_none():
    """If _run_diff fails (returns None), change_pct and diff stay None."""
    with tempfile.TemporaryDirectory() as tmp:
        before, after, out = _make_dirs(tmp)
        rel = os.path.join("mytest-viewport-test-tsx", "mytest-375px.png")
        _write_png(os.path.join(before, rel))
        os.makedirs(os.path.dirname(os.path.join(after, rel)), exist_ok=True)
        with open(os.path.join(after, rel), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"\xff" * 8)

        with patch.object(viewport_manifest, "_run_diff", return_value=None):
            viewport_manifest.build(before, after, "main", "run-5", out)

        manifest = _load(out)
        p = manifest["pairs"][0]
        assert p["status"] == "changed"
        assert p["change_pct"] is None
        assert p["diff"] is None


def test_build_summary_counts():
    """Summary counts must reflect actual pair statuses."""
    with tempfile.TemporaryDirectory() as tmp:
        before, after, out = _make_dirs(tmp)

        def png_before(name: str) -> None:
            _write_png(os.path.join(before, "t-viewport-test-tsx", f"{name}-375px.png"))

        def png_after(name: str, different: bool = False) -> None:
            path = os.path.join(after, "t-viewport-test-tsx", f"{name}-375px.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + (b"\xff" * 8 if different else b"\x00" * 8))

        png_before("same")
        png_after("same")           # unchanged (identical bytes)
        png_before("changed")
        png_after("changed", True)  # changed (different bytes)
        png_before("removed")       # removed (no after)
        png_after("newone")         # new (no before)

        with patch.object(viewport_manifest, "_run_diff", return_value=None):
            viewport_manifest.build(before, after, "main", "run-6", out)

        manifest = _load(out)
        s = manifest["summary"]
        assert s["changed"] == 1
        assert s["unchanged"] == 1
        assert s["new"] == 1
        assert s["removed"] == 1
