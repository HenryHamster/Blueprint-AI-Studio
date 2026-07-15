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
import http.server
import inspect
import os
import resource
import sys
import threading
import time

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

_SCRIPTS_DIR = next(
    (
        candidate
        for candidate in (
            _Path(__file__).resolve().parents[2] / "scripts",
            _Path("/repo/scripts"),  # backend test harness mounts the repo here
        )
        if candidate.is_dir()
    ),
    None,
)
assert _SCRIPTS_DIR is not None, "scripts/local_runner.py must be available to runner tests"
if str(_SCRIPTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPTS_DIR))

import local_runner  # noqa: E402


def _run_stream(artifact_dir, state, *, upload_return=200):
    """Drive ``_stream_viewable_files`` with a mocked uploader; return recorded calls.

    ``upload_return`` may be an int status, ``None`` (network error), or a
    zero-arg callable returning either (to vary the status per call).
    """
    calls: list = []

    def fake_upload(orch, token, job_id, relpath, path, size, content_type):
        calls.append(
            {"relpath": relpath, "path": path, "size": size, "content_type": content_type}
        )
        return upload_return() if callable(upload_return) else upload_return

    with mock.patch.object(local_runner, "_upload_viewable", side_effect=fake_upload):
        local_runner._stream_viewable_files(
            "http://orch", "tok", "job-1", str(artifact_dir), state
        )
        if state.thread is not None:
            state.thread.join(timeout=1)
        state.reap()
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


class TestRunnerViewableMultipartStream:
    """The uploader reads O_NOFOLLOW descriptors in bounded chunks."""

    def test_refuses_symlink_toctou(self, tmp_path):
        secret = tmp_path / "secret.bin"
        secret.write_bytes(b"operator secret")
        art = tmp_path / "artifacts"
        art.mkdir()
        _make_symlink_or_skip(str(secret), str(art / "chart.png"))
        chunks = list(local_runner._multipart_viewable_chunks(
            str(art / "chart.png"), 15, 1 << 20, "boundary", "chart.png", "image/png"
        ))
        assert chunks == []

    def test_512m_sparse_video_is_chunked_without_buffering(self, tmp_path):
        p = tmp_path / "rollout.mp4"
        size = 512 * 1024 * 1024
        with open(p, "wb") as fh:
            fh.truncate(size)
        chunks = local_runner._multipart_viewable_chunks(
            str(p), size, size, "boundary", "rollout.mp4", "video/mp4"
        )
        headers = [next(chunks) for _ in range(6)]
        first_payload = next(chunks)
        assert sum(map(len, headers)) < 1024
        assert len(first_payload) == local_runner._VIEWABLE_UPLOAD_CHUNK_BYTES
        assert local_runner._viewable_multipart_length(
            "boundary", "rollout.mp4", size, "video/mp4"
        ) == sum(map(len, headers)) + size + len(b"\r\n--boundary--\r\n")


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
        calls = []
        for _ in range(local_runner._MAX_VIEWABLE_FILE_COUNT):
            calls.extend(_run_stream(art, state))
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

    def test_returns_status_on_success(self, tmp_path):
        p = tmp_path / "reward.png"
        p.write_bytes(b"\x89PNG")
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with mock.patch.object(local_runner.urllib.request, "urlopen", return_value=resp):
            status = local_runner._upload_viewable(
                "http://orch", "tok", "job-1", "reward.png", str(p), 4, "image/png"
            )
        assert status == 200

    def test_returns_code_on_http_error(self, tmp_path):
        p = tmp_path / "reward.png"
        p.write_bytes(b"\x89PNG")
        err = local_runner.urllib.error.HTTPError(
            "http://orch", 409, "terminal", {}, None
        )
        with mock.patch.object(local_runner.urllib.request, "urlopen", side_effect=err):
            status = local_runner._upload_viewable(
                "http://orch", "tok", "job-1", "reward.png", str(p), 4, "image/png"
            )
        assert status == 409

    def test_returns_none_on_network_error(self, tmp_path):
        p = tmp_path / "reward.png"
        p.write_bytes(b"\x89PNG")
        err = local_runner.urllib.error.URLError("connection refused")
        with mock.patch.object(local_runner.urllib.request, "urlopen", side_effect=err):
            status = local_runner._upload_viewable(
                "http://orch", "tok", "job-1", "reward.png", str(p), 4, "image/png"
            )
        assert status is None

    def test_scan_to_open_size_change_does_not_contact_receiver(self, tmp_path):
        """A stale scan result is rejected before it can advertise Content-Length."""
        p = tmp_path / "reward.png"
        p.write_bytes(b"x" * 1024)
        p.write_bytes(b"x" * 8)  # mutation after the scan supplied size=1024
        with mock.patch.object(local_runner.urllib.request, "urlopen") as urlopen:
            status = local_runner._upload_viewable(
                "http://127.0.0.1:1", "tok", "job-1", "reward.png", str(p), 1024,
                "image/png",
            )
        assert status is None
        urlopen.assert_not_called()

    def test_short_read_aborts_real_receiver_without_waiting_for_content_length(self, tmp_path, monkeypatch):
        """A transfer-time truncate closes a real receiver instead of timing out."""
        class Receiver(http.server.BaseHTTPRequestHandler):
            started = threading.Event()
            finished = threading.Event()
            received = 0
            eof = False

            def do_POST(self):  # noqa: N802 - required stdlib handler spelling
                self.connection.settimeout(3)
                expected = int(self.headers["Content-Length"])
                type(self).started.set()
                try:
                    while type(self).received < expected:
                        chunk = self.rfile.read(min(64 * 1024, expected - type(self).received))
                        if not chunk:
                            type(self).eof = True
                            break
                        type(self).received += len(chunk)
                except TimeoutError:
                    pass
                finally:
                    type(self).finished.set()

            def log_message(self, *_args):
                pass

        p = tmp_path / "rollout.mp4"
        size = 64 * 1024 * 1024
        with open(p, "wb") as fh:
            fh.truncate(size)
        monkeypatch.setattr(local_runner, "_VIEWABLE_UPLOAD_CHUNK_BYTES", 64 * 1024)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def truncate_after_request_starts():
            assert Receiver.started.wait(2), "request never reached receiver"
            with open(p, "r+b") as fh:
                fh.truncate(0)

        truncator = threading.Thread(target=truncate_after_request_starts, daemon=True)
        truncator.start()
        try:
            status = local_runner._upload_viewable(
                f"http://127.0.0.1:{server.server_port}", "tok", "job-1", "rollout.mp4",
                str(p), size, "video/mp4",
            )
            assert status is None
            assert Receiver.finished.wait(2), "receiver waited for the advertised body"
            assert Receiver.eof
            assert Receiver.received < size
        finally:
            truncator.join(timeout=1)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)

    def test_512m_slow_receiver_keeps_compute_heartbeats_and_cancel_checks_cadenced(
        self, tmp_path
    ):
        """A real throttled upload must not block the compute-job heartbeat thread.

        This exercises the production multipart generator and ``urlopen`` with a
        512 MiB sparse video.  The receiver deliberately consumes it slowly while
        the real compute loop continues to issue running reports (which are also
        its cancellation checks).  Sampling process RSS catches reintroduction of
        a full-file/multipart buffer without requiring 512 MiB of test storage.
        """

        class SlowReceiver(http.server.BaseHTTPRequestHandler):
            received = 0
            started = threading.Event()
            stop = threading.Event()

            def do_POST(self):  # noqa: N802 - required stdlib handler spelling
                type(self).started.set()
                remaining = int(self.headers["Content-Length"])
                while remaining and not type(self).stop.is_set():
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    type(self).received += len(chunk)
                    remaining -= len(chunk)
                    time.sleep(0.002)  # ~32 MiB/s: enough backpressure to block send
                if not type(self).stop.is_set():
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowReceiver)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        reports: list[float] = []
        rss_samples: list[int] = []
        sampling = threading.Event()

        def sample_rss() -> None:
            while not sampling.is_set():
                rss_samples.append(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                time.sleep(0.01)

        def fake_report(_orch, _token, _job_id, *, exec_status, **_kwargs):
            if exec_status == "running":
                reports.append(time.monotonic())
            return False  # the runner performs a cancellation check on each report

        def fake_plan(**kwargs):
            artifact_dir = kwargs["artifact_dir_host"]
            script = (
                "import os, time; "
                "open(os.path.join(os.environ['ARTIFACT_DIR'], 'rollout.mp4'), 'wb').truncate("
                f"{512 * 1024 * 1024}); time.sleep(0.8)"
            )
            return type("Plan", (), {
                "declined": False, "backend": "test", "argv": [sys.executable, "-c", script],
                "env": {**os.environ, "ARTIFACT_DIR": artifact_dir}, "container_name": "test-job",
            })()

        class FakeCaps:
            has_gpu = False

        baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        sampler = threading.Thread(target=sample_rss, daemon=True)
        sampler.start()
        try:
            with mock.patch.object(local_runner.iso, "decide_compute_backend", return_value="test"), \
                 mock.patch.object(local_runner.iso, "plan_compute_execution", side_effect=fake_plan), \
                 mock.patch.object(local_runner, "_report_compute_job", side_effect=fake_report), \
                 mock.patch.object(local_runner, "_RUNNER_HEARTBEAT_INTERVAL", 0.1):
                local_runner._run_compute_job(
                    {"job_id": "slow-upload", "command": "unused", "env": {}},
                    f"http://127.0.0.1:{server.server_port}", "tok", set(), threading.Lock(),
                    capabilities=FakeCaps(),
                )
            assert SlowReceiver.started.wait(1), "the real urlopen upload never reached receiver"
            assert SlowReceiver.received >= 1024 * 1024, "receiver did not observe streamed video bytes"
            heartbeat_times = reports[1:]  # first running report is the post-launch acknowledgement
            assert len(heartbeat_times) >= 4, "expected repeated report/cancellation checks during upload"
            assert max(b - a for a, b in zip(heartbeat_times, heartbeat_times[1:])) < 0.25
            assert max(rss_samples, default=baseline_rss) - baseline_rss < 128 * 1024, (
                "512 MiB preview must not be materialized in the runner process"
            )
        finally:
            sampling.set()
            sampler.join(timeout=1)
            SlowReceiver.stop.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)


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


def test_run_compute_job_drains_delayed_final_viewables_before_terminal_report():
    """Delayed final uploads complete before terminal reporting."""
    uploads: list[str] = []
    reports: list[str] = []
    upload_started = threading.Event()
    allow_uploads = threading.Event()

    def fake_plan(**kwargs):
        artifact_dir = kwargs["artifact_dir_host"]
        script = (
            "import os; "
            "open(os.path.join(os.environ['ARTIFACT_DIR'], 'a.png'), 'wb').write(b'a'); "
            "open(os.path.join(os.environ['ARTIFACT_DIR'], 'b.png'), 'wb').write(b'b')"
        )
        return type("Plan", (), {"declined": False, "backend": "test",
            "argv": [sys.executable, "-c", script], "env": {**os.environ, "ARTIFACT_DIR": artifact_dir},
            "container_name": "test-job"})()

    def fake_upload(*args):
        uploads.append(args[3])
        upload_started.set()
        assert allow_uploads.wait(2)
        return 200

    def fake_report(_orch, _token, _job_id, *, exec_status, **_kwargs):
        reports.append(exec_status)
        return False

    with mock.patch.object(local_runner.iso, "decide_compute_backend", return_value="test"), \
         mock.patch.object(local_runner.iso, "plan_compute_execution", side_effect=fake_plan), \
         mock.patch.object(local_runner, "_upload_viewable", side_effect=fake_upload), \
         mock.patch.object(local_runner, "_upload_artifact"), \
         mock.patch.object(local_runner, "_report_compute_job", side_effect=fake_report), \
         mock.patch.object(local_runner, "_RUNNER_HEARTBEAT_INTERVAL", 0.1):
        worker = threading.Thread(target=local_runner._run_compute_job,
            args=({"job_id": "final-viewables", "command": "unused", "env": {}},
                  "http://orch", "tok", set(), threading.Lock()),
            kwargs={"capabilities": type("Caps", (), {"has_gpu": False})()},
        )
        worker.start()
        assert upload_started.wait(2)
        assert not any(status in {"succeeded", "failed"} for status in reports)
        allow_uploads.set()
        worker.join(timeout=3)

    assert not worker.is_alive()
    assert uploads == ["a.png", "b.png"]
    assert reports[-1] == "succeeded"


def test_run_compute_job_bounds_slow_final_viewable_drain():
    """A stuck best-effort preview cannot hold the terminal report or runner slot."""
    reports: list[str] = []
    upload_started = threading.Event()
    release_upload = threading.Event()

    def fake_plan(**kwargs):
        artifact_dir = kwargs["artifact_dir_host"]
        script = (
            "import os; "
            "open(os.path.join(os.environ['ARTIFACT_DIR'], 'final.png'), 'wb').write(b'x')"
        )
        return type("Plan", (), {"declined": False, "backend": "test",
            "argv": [sys.executable, "-c", script], "env": {**os.environ, "ARTIFACT_DIR": artifact_dir},
            "container_name": "test-job"})()

    def fake_upload(*_args):
        upload_started.set()
        release_upload.wait(2)
        return 200

    def fake_report(_orch, _token, _job_id, *, exec_status, **_kwargs):
        reports.append(exec_status)
        return False

    try:
        with mock.patch.object(local_runner.iso, "decide_compute_backend", return_value="test"), \
             mock.patch.object(local_runner.iso, "plan_compute_execution", side_effect=fake_plan), \
             mock.patch.object(local_runner, "_upload_viewable", side_effect=fake_upload), \
             mock.patch.object(local_runner, "_upload_artifact"), \
             mock.patch.object(local_runner, "_report_compute_job", side_effect=fake_report), \
             mock.patch.object(local_runner, "_RUNNER_HEARTBEAT_INTERVAL", 0.1), \
             mock.patch.object(local_runner, "_FINAL_VIEWABLE_DRAIN_SECONDS", 0.05):
            worker = threading.Thread(target=local_runner._run_compute_job,
                args=({"job_id": "bounded-final-viewable", "command": "unused", "env": {}},
                      "http://orch", "tok", set(), threading.Lock()),
                kwargs={"capabilities": type("Caps", (), {"has_gpu": False})()},
            )
            worker.start()
            assert upload_started.wait(2)
            worker.join(timeout=1)

        assert not worker.is_alive()
        assert reports[-1] == "succeeded"
    finally:
        release_upload.set()
