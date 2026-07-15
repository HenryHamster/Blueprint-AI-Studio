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
        selected, size_dropped = _collect_viewable_candidates_from_dir(str(art))
        assert size_dropped == 0
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
        assert _collect_viewable_candidates_from_dir("") == ([], 0)
        assert (
            _collect_viewable_candidates_from_dir(str(tmp_path / "missing"))
            == ([], 0)
        )
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "empty.png").write_bytes(b"")
        # A zero-byte viewable file is skipped but NOT counted as a size-cap
        # omission (it was not omitted for being too large).
        assert _collect_viewable_candidates_from_dir(str(art)) == ([], 0)

    def test_oversize_viewable_file_counted_as_dropped(self, tmp_path):
        """A viewable file over the 8 MiB per-file cap is excluded from the
        candidates AND increments the size-dropped tally, while an
        under-cap viewable file and a plain non-viewable file do not."""
        from app.compute_backends import _MAX_VIEWABLE_FILE_BYTES

        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "small.png").write_bytes(b"\x89PNG" + b"x" * 32)
        (art / "huge.png").write_bytes(b"\x89PNG" + b"y" * (_MAX_VIEWABLE_FILE_BYTES + 1))
        (art / "notes.txt").write_bytes(b"z" * (_MAX_VIEWABLE_FILE_BYTES + 1))
        selected, size_dropped = _collect_viewable_candidates_from_dir(str(art))
        assert [c.filename for c in selected] == ["small.png"]
        assert size_dropped == 1

    def test_count_and_size_cap_both_reflected(self, tmp_path):
        """When files exceed BOTH the 25-file count cap and the size cap, the
        dropped tally reported by ``_collect_viewable_artifact_candidates`` is
        the sum of both (via the size-dropped signal returned here)."""
        from app.compute_backends import (
            _MAX_VIEWABLE_FILE_BYTES,
            _MAX_VIEWABLE_FILE_COUNT,
        )

        art = tmp_path / "artifacts"
        art.mkdir()
        # count-cap overflow: (count + 3) small viewable files
        for i in range(_MAX_VIEWABLE_FILE_COUNT + 3):
            (art / f"plot_{i:03d}.png").write_bytes(b"\x89PNG" + b"x" * 16)
        # size-cap omissions: 2 oversize viewable files
        (art / "big_a.png").write_bytes(b"\x89PNG" + b"y" * (_MAX_VIEWABLE_FILE_BYTES + 1))
        (art / "big_b.png").write_bytes(b"\x89PNG" + b"y" * (_MAX_VIEWABLE_FILE_BYTES + 1))
        selected, size_dropped = _collect_viewable_candidates_from_dir(str(art))
        # oversize files never appear in candidates
        assert len(selected) == _MAX_VIEWABLE_FILE_COUNT + 3
        assert size_dropped == 2


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

        selected, _ = _collect_viewable_candidates_from_dir(str(art))
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

        selected, _ = _collect_viewable_candidates_from_dir(str(art))
        assert [c.filename for c in selected] == ["ok.png"], (
            "walk must not descend through a symlinked dir"
        )

    def test_collect_refuses_symlinked_artifact_dir(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.png").write_bytes(b"x" * 10)
        link = tmp_path / "artifacts"
        _make_symlink_or_skip(str(outside), str(link))
        assert _collect_viewable_candidates_from_dir(str(link)) == ([], 0)

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


# ───────────────────────────────────────────────────────────────────────────
# Runner-side mid-run viewable streaming (scripts/local_runner.py)
#
# The server-side scan above (app.compute_backends) runs INSIDE the orchestrator
# for local/local-gpu backends. The runner-subprocess backend, by contrast, runs
# the job on operator hardware under scripts/local_runner.py — a standalone,
# stdlib-only daemon that CANNOT import app.*. It therefore carries its OWN inlined
# viewable classifier/scanner mirroring the policy above, and streams NEW-or-CHANGED
# viewable files to POST /api/runner/compute-job/{job_id}/viewable WHILE the job
# runs, so reward-curve plots / rollout frames appear live in the strategic canvas.
# These are the regression tests for that runner-side path.
# ───────────────────────────────────────────────────────────────────────────

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
from unittest import mock  # noqa: E402

_SCRIPTS_DIR = _Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPTS_DIR))

import local_runner  # noqa: E402


def _run_stream(artifact_dir, state, *, upload_return=200):
    """Drive ``_stream_viewable_files`` with a mocked uploader; return recorded calls.

    ``upload_return`` may be an int status, ``None`` (network error), or a
    zero-arg callable returning either (to vary the status per call).
    """
    calls: list = []

    def fake_upload(orch, token, job_id, relpath, data, content_type):
        calls.append(
            {"relpath": relpath, "data": data, "content_type": content_type}
        )
        return upload_return() if callable(upload_return) else upload_return

    with mock.patch.object(local_runner, "_upload_viewable", side_effect=fake_upload):
        local_runner._stream_viewable_files(
            "http://orch", "tok", "job-1", str(artifact_dir), state
        )
    return calls


class TestRunnerViewableScan:
    """The runner's inlined scan mirrors the server allowlist/kinds/symlink policy."""

    def test_scan_classifies_allowlist_and_excludes_svg(self, tmp_path):
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
            "chart.svg",  # EXCLUDED by design (script-bearing SVG is an XSS vector)
            "index.html",
            "results.json",
            "noext",
        ):
            (art / name).write_bytes(b"x" * 16)
        by_name = {c["relpath"]: c["kind"] for c in local_runner._scan_viewable_files(str(art))}
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

    def test_scan_content_types(self, tmp_path):
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "a.png").write_bytes(b"x" * 8)
        (art / "b.mp4").write_bytes(b"y" * 8)
        by_name = {
            c["relpath"]: c["content_type"]
            for c in local_runner._scan_viewable_files(str(art))
        }
        assert by_name == {"a.png": "image/png", "b.mp4": "video/mp4"}

    def test_scan_empty_zero_byte_and_missing_dir(self, tmp_path):
        assert local_runner._scan_viewable_files("") == []
        assert local_runner._scan_viewable_files(str(tmp_path / "missing")) == []
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "empty.png").write_bytes(b"")  # zero-byte viewable file skipped
        assert local_runner._scan_viewable_files(str(art)) == []

    def test_scan_keeps_subdir_relpath_and_sorts(self, tmp_path):
        art = tmp_path / "artifacts"
        (art / "arm2").mkdir(parents=True)
        (art / "arm1").mkdir()
        (art / "arm2" / "reward.png").write_bytes(b"x" * 8)
        (art / "arm1" / "reward.png").write_bytes(b"x" * 8)
        (art / "top.png").write_bytes(b"x" * 8)
        relpaths = [c["relpath"] for c in local_runner._scan_viewable_files(str(art))]
        assert relpaths == ["arm1/reward.png", "arm2/reward.png", "top.png"]

    def test_scan_skips_symlinked_file(self, tmp_path):
        secret = tmp_path / "strategic-runner.env"
        secret.write_bytes(b"BLUEPRINT_RUNNER_TOKEN=leaked")
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "real.png").write_bytes(b"\x89PNG real")
        _make_symlink_or_skip(str(secret), str(art / "loss_curve.png"))
        relpaths = [c["relpath"] for c in local_runner._scan_viewable_files(str(art))]
        assert relpaths == ["real.png"]

    def test_scan_skips_symlinked_directory(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.png").write_bytes(b"x" * 10)
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "ok.png").write_bytes(b"y" * 10)
        _make_symlink_or_skip(str(outside), str(art / "plots"))
        relpaths = [c["relpath"] for c in local_runner._scan_viewable_files(str(art))]
        assert relpaths == ["ok.png"]

    def test_scan_refuses_symlinked_artifact_dir(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.png").write_bytes(b"x" * 10)
        link = tmp_path / "artifacts"
        _make_symlink_or_skip(str(outside), str(link))
        assert local_runner._scan_viewable_files(str(link)) == []


class TestRunnerViewableRead:
    """``_read_viewable_bytes`` is the O_NOFOLLOW read used before each upload."""

    def test_reads_regular_file(self, tmp_path):
        p = tmp_path / "plot.png"
        p.write_bytes(b"\x89PNG data")
        assert local_runner._read_viewable_bytes(str(p), 1 << 20) == b"\x89PNG data"

    def test_refuses_symlink_toctou(self, tmp_path):
        secret = tmp_path / "secret.bin"
        secret.write_bytes(b"operator secret")
        art = tmp_path / "artifacts"
        art.mkdir()
        _make_symlink_or_skip(str(secret), str(art / "chart.png"))
        assert local_runner._read_viewable_bytes(str(art / "chart.png"), 1 << 20) is None

    def test_refuses_file_over_cap(self, tmp_path):
        p = tmp_path / "big.png"
        p.write_bytes(b"z" * 100)
        assert local_runner._read_viewable_bytes(str(p), 50) is None


class TestRunnerViewableStreaming:
    """The heartbeat-cadence uploader: dedup, overwrite-refresh, caps, budget, and
    non-fatal handling of every upload failure."""

    def test_new_file_uploaded_once_then_not_resent(self, tmp_path):
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "reward.png").write_bytes(b"\x89PNG one")
        state = local_runner._ViewableStreamState()

        first = _run_stream(art, state)
        assert [c["relpath"] for c in first] == ["reward.png"]
        assert first[0]["content_type"] == "image/png"

        # Same (mtime, size) → unchanged → not re-uploaded on the next heartbeat.
        second = _run_stream(art, state)
        assert second == []

    def test_overwritten_file_is_resent(self, tmp_path):
        art = tmp_path / "artifacts"
        art.mkdir()
        f = art / "reward_curve.png"
        f.write_bytes(b"\x89PNG one")
        state = local_runner._ViewableStreamState()
        assert len(_run_stream(art, state)) == 1

        # Rewrite with a DIFFERENT size and a bumped mtime (eval step overwrites it).
        f.write_bytes(b"\x89PNG two-longer-content")
        os.utime(str(f), (f.stat().st_atime, f.stat().st_mtime + 10))
        second = _run_stream(art, state)
        assert [c["relpath"] for c in second] == ["reward_curve.png"]

    def test_oversize_file_skipped_without_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_runner, "_MAX_VIEWABLE_IMAGE_BYTES", 64)
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "huge.png").write_bytes(b"x" * 128)  # over the (patched) 64-byte cap
        (art / "small.png").write_bytes(b"x" * 16)
        state = local_runner._ViewableStreamState()
        calls = _run_stream(art, state)
        assert [c["relpath"] for c in calls] == ["small.png"]

    def test_over_budget_new_files_skipped(self, tmp_path):
        art = tmp_path / "artifacts"
        art.mkdir()
        for i in range(local_runner._MAX_VIEWABLE_FILE_COUNT + 2):
            (art / f"plot_{i:03d}.png").write_bytes(b"x" * 16)
        state = local_runner._ViewableStreamState()
        calls = _run_stream(art, state)
        assert len(calls) == local_runner._MAX_VIEWABLE_FILE_COUNT
        # An ALREADY-sent file that is overwritten still re-uploads despite budget.
        first_relpath = calls[0]["relpath"]
        f = art / first_relpath
        f.write_bytes(b"x" * 32)
        os.utime(str(f), (f.stat().st_atime, f.stat().st_mtime + 10))
        second = _run_stream(art, state)
        assert [c["relpath"] for c in second] == [first_relpath]

    def test_upload_failure_is_swallowed_and_retried(self, tmp_path):
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "reward.png").write_bytes(b"\x89PNG")
        state = local_runner._ViewableStreamState()

        # A 500 (or 409/429/None) must not raise AND must not record the signature,
        # so the next heartbeat retries the still-unsent file.
        for bad in (500, 409, 429, 413, None):
            calls = _run_stream(art, state, upload_return=bad)
            assert [c["relpath"] for c in calls] == ["reward.png"]
        assert state.sent == {}  # never recorded on a non-2xx / network error

        # A subsequent 2xx records it; then it stops re-sending.
        assert len(_run_stream(art, state, upload_return=201)) == 1
        assert _run_stream(art, state, upload_return=201) == []

    def test_scan_error_is_swallowed(self, tmp_path):
        state = local_runner._ViewableStreamState()
        with mock.patch.object(
            local_runner, "_scan_viewable_files", side_effect=OSError("boom")
        ):
            # Must not raise — mid-run streaming is strictly best-effort.
            local_runner._stream_viewable_files(
                "http://orch", "tok", "job-1", str(tmp_path), state
            )


class TestRunnerViewableUpload:
    """``_upload_viewable`` maps transport outcomes to a status/None, never raising."""

    def test_returns_status_on_success(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with mock.patch.object(local_runner.urllib.request, "urlopen", return_value=resp):
            status = local_runner._upload_viewable(
                "http://orch", "tok", "job-1", "reward.png", b"\x89PNG", "image/png"
            )
        assert status == 200

    def test_returns_code_on_http_error(self):
        err = local_runner.urllib.error.HTTPError(
            "http://orch", 409, "terminal", {}, None
        )
        with mock.patch.object(local_runner.urllib.request, "urlopen", side_effect=err):
            status = local_runner._upload_viewable(
                "http://orch", "tok", "job-1", "reward.png", b"\x89PNG", "image/png"
            )
        assert status == 409

    def test_returns_none_on_network_error(self):
        err = local_runner.urllib.error.URLError("connection refused")
        with mock.patch.object(local_runner.urllib.request, "urlopen", side_effect=err):
            status = local_runner._upload_viewable(
                "http://orch", "tok", "job-1", "reward.png", b"\x89PNG", "image/png"
            )
        assert status is None


def test_run_compute_job_flushes_viewables_before_terminal_handoff():
    """A final scan must run after exit and before the terminal job report.

    This preserves outputs written immediately before a short command exits,
    including videos too large for the terminal artifact bundle.
    """
    source = inspect.getsource(local_runner)
    tree = ast.parse(source)
    run_job = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run_compute_job"
        ),
        None,
    )
    assert run_job is not None, "_run_compute_job missing"
    final_stream_call = source.index(
        "# A command can create its final plot or rollout video immediately before"
    )
    terminal_report = source.index(
        "exec_status=exec_status_final,", final_stream_call
    )
    assert final_stream_call < terminal_report
    final_segment = source[final_stream_call:terminal_report]
    assert "_stream_viewable_files(" in final_segment
    assert "viewable_state" in final_segment
