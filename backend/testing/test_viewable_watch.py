"""Behavioral tests for backend/app/_compute_scripts/viewable_watch.py.

Covers: directory scan, extension filtering, marker-based dedup, HTTP upload
success/failure/cap paths, and the STOP-on-429 early exit in main().

No DB, network, or Docker required — all I/O is faked with tmp_path and
unittest.mock patches.
"""

import importlib.util
import os
import pathlib
import urllib.error
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import the standalone script as a module without adding it to sys.modules
# under its filesystem name.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    pathlib.Path(__file__).parent.parent
    / "app"
    / "_compute_scripts"
    / "viewable_watch.py"
)

_spec = importlib.util.spec_from_file_location("viewable_watch", _SCRIPT_PATH)
_vw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vw)

_viewable_files = _vw._viewable_files
_marker_path = _vw._marker_path
_was_uploaded = _vw._was_uploaded
_mark_uploaded = _vw._mark_uploaded
_upload_file = _vw._upload_file
main = _vw.main


# ---------------------------------------------------------------------------
# _viewable_files
# ---------------------------------------------------------------------------


def test_viewable_files_yields_matching_extensions(tmp_path):
    """png/jpg/jpeg/webp/gif/mp4/webm are included; txt/pdf/svg are not."""
    (tmp_path / "plot.png").write_bytes(b"x")
    (tmp_path / "clip.mp4").write_bytes(b"x")
    (tmp_path / "frame.webp").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "report.pdf").write_bytes(b"x")
    (tmp_path / "figure.svg").write_bytes(b"x")

    found = {relpath for relpath, _ in _viewable_files(str(tmp_path))}
    assert "plot.png" in found
    assert "clip.mp4" in found
    assert "frame.webp" in found
    assert "notes.txt" not in found
    assert "report.pdf" not in found
    assert "figure.svg" not in found


def test_viewable_files_empty_dir_yields_nothing(tmp_path):
    assert list(_viewable_files(str(tmp_path))) == []


def test_viewable_files_nonexistent_dir_yields_nothing(tmp_path):
    assert list(_viewable_files(str(tmp_path / "does_not_exist"))) == []


def test_viewable_files_preserves_subdir_relpath(tmp_path):
    """Files in subdirectories are yielded with their relative path intact."""
    subdir = tmp_path / "arm1"
    subdir.mkdir()
    (subdir / "reward.png").write_bytes(b"x")

    relpaths = [relpath for relpath, _ in _viewable_files(str(tmp_path))]
    assert os.path.join("arm1", "reward.png") in relpaths


# ---------------------------------------------------------------------------
# _marker_path / _was_uploaded / _mark_uploaded
# ---------------------------------------------------------------------------


def test_marker_path_sanitizes_slashes(tmp_path):
    """Forward slashes in relpath become __ in the marker filename."""
    mp = _marker_path(str(tmp_path), "arm1/reward.png")
    assert os.path.basename(mp) == "arm1__reward.png.uploaded"


def test_was_uploaded_and_mark_uploaded_round_trip(tmp_path):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir)
    relpath = "subdir/plot.png"
    assert not _was_uploaded(state_dir, relpath)
    _mark_uploaded(state_dir, relpath)
    assert _was_uploaded(state_dir, relpath)


# ---------------------------------------------------------------------------
# _upload_file — HTTP outcome coverage
# ---------------------------------------------------------------------------


def _fake_response(status_code):
    resp = MagicMock()
    resp.status = status_code
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_upload_file_success_returns_true(tmp_path):
    """HTTP 200 → True."""
    f = tmp_path / "plot.png"
    f.write_bytes(b"fake-png-data")
    with patch("urllib.request.urlopen", return_value=_fake_response(200)):
        result = _upload_file("https://orch.example.com", "job-123", "tok", "plot.png", str(f))
    assert result is True


def test_upload_file_http_413_returns_true_skip(tmp_path):
    """HTTP 413 (oversized) → True (skip, not retried)."""
    f = tmp_path / "big.mp4"
    f.write_bytes(b"data")
    exc = urllib.error.HTTPError(url="x", code=413, msg="Too Large", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        result = _upload_file("https://orch.example.com", "job-123", "tok", "big.mp4", str(f))
    assert result is True


def test_upload_file_http_429_returns_stop(tmp_path, capsys):
    """HTTP 429 (cap reached) → 'STOP' sentinel and a stderr message."""
    f = tmp_path / "plot.png"
    f.write_bytes(b"data")
    exc = urllib.error.HTTPError(url="x", code=429, msg="Too Many Requests", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        result = _upload_file("https://orch.example.com", "job-123", "tok", "plot.png", str(f))
    assert result == "STOP"
    captured = capsys.readouterr()
    assert "cap reached" in captured.err


def test_upload_file_http_404_returns_true_permanent_skip(tmp_path):
    """HTTP 404 (job gone) → True (permanent skip, not retried — same as 413)."""
    f = tmp_path / "plot.png"
    f.write_bytes(b"data")
    exc = urllib.error.HTTPError(url="x", code=404, msg="Not Found", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        result = _upload_file("https://orch.example.com", "job-123", "tok", "plot.png", str(f))
    assert result is True


def test_upload_file_network_error_returns_false(tmp_path):
    """URLError (network failure) → False."""
    f = tmp_path / "plot.png"
    f.write_bytes(b"data")
    exc = urllib.error.URLError(reason="connection refused")
    with patch("urllib.request.urlopen", side_effect=exc):
        result = _upload_file("https://orch.example.com", "job-123", "tok", "plot.png", str(f))
    assert result is False


def test_upload_file_missing_file_returns_false(tmp_path):
    """File missing from disk → False (OSError in open)."""
    result = _upload_file(
        "https://orch.example.com", "job-123", "tok",
        "ghost.png", str(tmp_path / "ghost.png"),
    )
    assert result is False


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


def test_main_deduplicates_on_restart(tmp_path):
    """A file marked uploaded in one pass is skipped in the next."""
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "plot.png").write_bytes(b"data")

    uploaded = []

    def fake_upload(orch_url, job_id, token, relpath, abspath):
        uploaded.append(relpath)
        return True

    argv = ["prog", str(artifact_dir), "https://orch.example.com", "job-dedup2", "tok"]
    state_dir = "/tmp/blueprint_viewable_watch_job-dedup2"
    os.makedirs(state_dir, exist_ok=True)
    try:
        # Pre-mark so the watcher treats the file as already uploaded.
        _mark_uploaded(state_dir, "plot.png")

        with (
            patch.object(_vw, "_upload_file", side_effect=fake_upload),
            patch.object(_vw, "time") as mock_time,
        ):
            call_count = [0]

            def stop_after_first(_):
                call_count[0] += 1
                raise StopIteration

            mock_time.sleep.side_effect = stop_after_first
            try:
                main(argv)
            except StopIteration:
                pass

        # The marker was already present, so upload should not have been called.
        assert "plot.png" not in uploaded
    finally:
        import shutil
        shutil.rmtree(state_dir, ignore_errors=True)


def test_main_stop_on_cap_reached(tmp_path):
    """When _upload_file returns 'STOP', main() returns 0 immediately."""
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "plot.png").write_bytes(b"data")

    argv = ["prog", str(artifact_dir), "https://orch.example.com", "job-stop", "tok"]

    with patch.object(_vw, "_upload_file", return_value="STOP"):
        result = main(argv)

    assert result == 0


def test_main_returns_1_on_missing_args():
    """Too few args → return 1."""
    result = main(["prog"])
    assert result == 1


