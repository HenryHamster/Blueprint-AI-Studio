"""Symlink hardening of the host-side viewable-artifact scan.

``_collect_viewable_candidates_from_dir`` in ``app.compute_backends`` selects a
terminal local/local-gpu compute job's viewable files (images/videos) by walking
the job's on-disk artifact dir on the HOST. That dir was writable by the
experiment command, so a planted symlink (e.g.
``loss_curve.png -> /etc/passwd`` or an operator credentials file) must never
be dereferenced and persisted as a viewer-accessible preview object.

These are the regression tests for that exfiltration guard: symlinked files
skipped, symlinked dirs never descended, realpath containment enforced, and the
read side opens with ``O_NOFOLLOW`` (TOCTOU: an entry swapped for a symlink
between scan and read is refused by the kernel, not just the earlier stat).

Standalone — no database.
"""

import ast
import inspect
import os

import pytest

from app.compute_backends import (
    _collect_viewable_candidates_from_dir,
    _is_under_artifact_root,
    _read_viewable_file_nofollow,
)


def _make_symlink_or_skip(target: str, link: str) -> None:
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except (OSError, NotImplementedError):  # e.g. Windows without privilege
        pytest.skip("symlink creation not permitted on this platform")


class TestViewableSelectionPolicy:
    """Extension allowlist / caps of the host-side dir scan."""

    def test_allowlist_and_kinds(self, tmp_path):
        art = tmp_path / "artifacts"
        art.mkdir()
        for name in (
            "plot.png",
            "photo.JPG",
            "anim.gif",
            "frame.webp",
            "shot.jpeg",
            "rollout.mp4",
            "clip.webm",
            "chart.svg",  # EXCLUDED by design: script execution via signed URL
            "index.html",
            "results.json",
            "noext",
        ):
            (art / name).write_bytes(b"x" * 16)
        selected = _collect_viewable_candidates_from_dir(str(art))
        by_name = {c.filename: c.kind for c in selected}
        assert by_name == {
            "plot.png": "image",
            "photo.JPG": "image",
            "anim.gif": "image",
            "frame.webp": "image",
            "shot.jpeg": "image",
            "rollout.mp4": "video",
            "clip.webm": "video",
        }
        assert "chart.svg" not in by_name

    def test_empty_zero_byte_and_missing_dir(self, tmp_path):
        assert _collect_viewable_candidates_from_dir("") == []
        assert (
            _collect_viewable_candidates_from_dir(str(tmp_path / "missing")) == []
        )
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "empty.png").write_bytes(b"")
        assert _collect_viewable_candidates_from_dir(str(art)) == []


class TestSymlinkExclusion:
    """The experiment wrote the artifact dir; the host-side scan must never
    dereference a planted symlink (operator-secret exfiltration via a
    viewer-accessible preview object)."""

    def test_collect_skips_symlinked_files(self, tmp_path):
        secret = tmp_path / "strategic-runner.env"
        secret.write_bytes(b"BLUEPRINT_RUNNER_TOKEN=leaked")
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "real.png").write_bytes(b"\x89PNG real")
        _make_symlink_or_skip(str(secret), str(art / "loss_curve.png"))

        selected = _collect_viewable_candidates_from_dir(str(art))
        assert [c.filename for c in selected] == ["real.png"], (
            "symlinked file must not be collected"
        )

    def test_collect_skips_symlinked_directories(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.png").write_bytes(b"x" * 10)
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "ok.png").write_bytes(b"y" * 10)
        _make_symlink_or_skip(str(outside), str(art / "plots"))

        selected = _collect_viewable_candidates_from_dir(str(art))
        assert [c.filename for c in selected] == ["ok.png"], (
            "walk must not descend through a symlinked dir"
        )

    def test_collect_refuses_symlinked_artifact_dir(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.png").write_bytes(b"x" * 10)
        link = tmp_path / "artifacts"
        _make_symlink_or_skip(str(outside), str(link))
        assert _collect_viewable_candidates_from_dir(str(link)) == []

    def test_read_nofollow_refuses_symlink_planted_after_scan(self, tmp_path):
        # TOCTOU: the entry could be a regular file at scan time and a symlink
        # by read time — the read-side re-check must refuse it.
        secret = tmp_path / "secret.bin"
        secret.write_bytes(b"operator secret")
        art = tmp_path / "artifacts"
        art.mkdir()
        _make_symlink_or_skip(str(secret), str(art / "chart.png"))

        root = os.path.realpath(str(art))
        assert _read_viewable_file_nofollow(str(art / "chart.png"), root) is None

    def test_read_nofollow_reads_regular_file(self, tmp_path):
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "plot.png").write_bytes(b"\x89PNG data")
        root = os.path.realpath(str(art))
        assert (
            _read_viewable_file_nofollow(str(art / "plot.png"), root)
            == b"\x89PNG data"
        )

    def test_read_nofollow_enforces_realpath_containment(self, tmp_path):
        # No symlink needed — runs on every platform: a path outside the
        # artifact root is refused even when it is a plain regular file.
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"data")
        other_root = os.path.realpath(str(tmp_path / "artifacts"))
        assert _read_viewable_file_nofollow(str(outside), other_root) is None

    def test_is_under_root_rejects_prefix_sibling(self):
        # Pure string check (islink-filter logic, platform-independent): a
        # sibling dir sharing the root as a name PREFIX must not pass.
        root = os.path.join(os.sep, "srv", "artifacts")
        assert _is_under_artifact_root(root, root)
        assert _is_under_artifact_root(os.path.join(root, "plots", "a.png"), root)
        assert not _is_under_artifact_root(
            os.path.join(os.sep, "srv", "artifacts-evil", "a.png"), root
        )
        assert not _is_under_artifact_root(os.path.join(os.sep, "srv"), root)


def test_dir_scan_source_invariants():
    """AST invariants on the scan path: the walk must pass
    ``followlinks=False``, and the collector must read bytes through the
    O_NOFOLLOW helper (no bare ``open()``)."""
    import app.compute_backends as compute_backends_mod

    source = inspect.getsource(compute_backends_mod)
    tree = ast.parse(source)

    def _find(name):
        return next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == name
            ),
            None,
        )

    collect = _find("_collect_viewable_candidates_from_dir")
    assert collect is not None, "_collect_viewable_candidates_from_dir missing"
    walk_calls = [
        node
        for node in ast.walk(collect)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "walk"
    ]
    assert walk_calls, "os.walk call missing from _collect_viewable_candidates_from_dir"
    assert all(
        any(
            kw.arg == "followlinks"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in call.keywords
        )
        for call in walk_calls
    ), (
        "os.walk in _collect_viewable_candidates_from_dir must pass "
        "followlinks=False"
    )
    bare_opens = [
        node
        for node in ast.walk(collect)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
    ]
    assert not bare_opens, (
        "_collect_viewable_candidates_from_dir must read artifact bytes via "
        "_read_viewable_file_nofollow, not a bare open() that follows symlinks"
    )

    reader = _find("_read_viewable_file_nofollow")
    assert reader is not None, "_read_viewable_file_nofollow missing"
    assert "O_NOFOLLOW" in ast.get_source_segment(source, reader), (
        "_read_viewable_file_nofollow must open with O_NOFOLLOW"
    )
