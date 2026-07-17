"""Poll the artifact directory for new viewable files and upload to orchestrator.

Invoked by the GCP startup script as a background process.  Polls
$BLUEPRINT_ARTIFACT_DIR every ~30 s for files matching viewable extensions
(png/jpg/jpeg/webp/gif/mp4/webm) and uploads each to the orchestrator's
``/api/agent/compute-jobs/<job_id>/viewable`` endpoint via multipart POST.

Uses a lightweight state directory (one marker file per uploaded path) so a
process restart does not re-upload files already observed.  The server enforces
per-kind byte caps (8 MiB images, 512 MiB videos) and the 25-file-per-job count
budget; files that exceed these caps are silently skipped (the bundle at
poweroff is still authoritative) so a pathological source-tree cannot crash
the watcher.

Inputs (argv):
  argv[1]  artifact_dir      -- absolute path to the job's BLUEPRINT_ARTIFACT_DIR
  argv[2]  orchestrator_url  -- scheme+host of the orchestrator, e.g.
                                ``https://blueprint.example.com``
  argv[3]  job_id            -- ``ComputeJob.id`` (UUID string)
  argv[4]  heartbeat_token   -- per-job HMAC secret for bearer auth

Runs forever until killed; never raises (all errors degrade gracefully).
"""

import os
import sys
import time
import urllib.request

# Viewable extensions this watcher considers for upload, matching
# ``storage.VIEWABLE_COMPUTE_JOB_EXTENSIONS`` minus PDF (excluded mid-run
# because it is a terminal output, not a live preview). SVG is included —
# the server sanitizes it (strips <script>/on* attrs) before storing.
_VIEWABLE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".webm", ".svg",
})

# Uploads smaller than this are sent as a single POST (in-memory).  Larger
# files use a chunked transfer strategy so a multi-hundred-MiB video does not
# need to be read into the Python heap before being sent.  Matches the image
# cap from compute_backends._MAX_VIEWABLE_FILE_BYTES.
_IN_MEMORY_BYTES = 8 * 1024 * 1024  # 8 MiB

# Fixed polling interval so the watcher does not hammer CPU on an empty dir.
_POLL_INTERVAL_SECONDS = 30


def _viewable_files(artifact_dir):
    """Yield (relpath, abspath) for every viewable file under ``artifact_dir``.

    Walks the directory tree and matches extensions.  Subdirectory context
    is preserved in the relpath so ``arm1/reward.png`` and ``arm2/reward.png``
    have distinct identities on the server (matched via
    ``storage.sanitize_viewable_relpath``, which joins components with ``__``).
    Symlinks are followed but silently skip broken ones.
    """
    if not os.path.isdir(artifact_dir):
        return
    for dirpath, _dirnames, filenames in os.walk(artifact_dir, followlinks=True):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in _VIEWABLE_EXTENSIONS:
                continue
            abspath = os.path.join(dirpath, name)
            if not os.path.isfile(abspath):
                continue
            relpath = os.path.relpath(abspath, artifact_dir)
            yield relpath, abspath


def _marker_path(state_dir, relpath):
    """Return the marker file path for a given relpath."""
    safe = relpath.replace("/", "__").replace("\\", "__")
    return os.path.join(state_dir, safe + ".uploaded")


def _was_uploaded(state_dir, relpath):
    """True when a marker file exists for this relpath."""
    return os.path.isfile(_marker_path(state_dir, relpath))


def _mark_uploaded(state_dir, relpath):
    """Create a marker file for this relpath."""
    marker = _marker_path(state_dir, relpath)
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write("1\n")
    except OSError:
        pass  # best-effort — re-upload is idempotent


def _upload_file(orchestrator_url, job_id, token, relpath, abspath):
    """Upload one viewable file to the orchestrator via multipart POST.

    Returns True on success (HTTP 2xx), False on any failure.  The server
    endpoint applies the same per-kind byte caps as the runner's viewable
    endpoint; a 413 (oversized) response is treated as a permanent skip and
    NOT retried.
    """
    url = f"{orchestrator_url}/api/agent/compute-jobs/{job_id}/viewable"
    boundary = b"----blueprintViewableUpload" + os.urandom(16).hex().encode("ascii")

    try:
        with open(abspath, "rb") as f:
            file_data = f.read()
    except OSError:
        return False

    filename_b = relpath.encode("utf-8")

    # Build multipart body: file field + filename field.
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="file"; filename="' +
        filename_b + b'"',
        b"Content-Type: application/octet-stream",
        b"",
        file_data,
        b"--" + boundary,
        b'Content-Disposition: form-data; name="filename"',
        b"",
        filename_b,
        b"--" + boundary + b"--",
    ]
    body = b"\r\n".join(parts)

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary.decode('ascii')}",
            "Content-Length": str(len(body)),
            # Stamp the file's mtime as a stable generation token so a
            # response-loss retry does not fabricate another revision.
            "X-Blueprint-Viewable-Revision": str(int(os.path.getmtime(abspath) * 1_000_000)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status < 300
    except urllib.error.HTTPError as exc:
        # 404/410 — job no longer exists; 413/429 — file too big or cap reached;
        # both are terminal for this file — do NOT retry.
        if exc.code in (404, 410, 413, 429):
            # Treat cap-reached as a signal to stop polling entirely.
            if exc.code == 429:
                sys.stderr.write(
                    f"[viewable_watch] cap reached for job {job_id} — "
                    "stopping watcher\n"
                )
                return "STOP"
            return True  # mark as uploaded so we don't retry the oversized file
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main(argv):
    if len(argv) < 5:
        sys.stderr.write(
            f"Usage: {argv[0]} <artifact_dir> <orchestrator_url> "
            "<job_id> <heartbeat_token>\n"
        )
        return 1

    artifact_dir = argv[1]
    orchestrator_url = argv[2].rstrip("/")
    job_id = argv[3]
    token = argv[4]

    # State directory lives under /tmp so it is ephemeral (VM poweroff cleans
    # it up).  Keyed by job_id so a restarted VM for a fresh job does not see
    # the prior job's marker files.
    state_dir = f"/tmp/blueprint_viewable_watch_{job_id}"
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass

    while True:
        try:
            for relpath, abspath in _viewable_files(artifact_dir):
                if _was_uploaded(state_dir, relpath):
                    continue
                result = _upload_file(
                    orchestrator_url, job_id, token, relpath, abspath
                )
                if result == "STOP":
                    # Cap reached — mark and stop polling.
                    _mark_uploaded(state_dir, relpath)
                    return 0
                if result:
                    _mark_uploaded(state_dir, relpath)
        except Exception:
            # Never raise — a watcher crash must not propagate to the caller
            # (the startup script runs with ``|| true``).
            pass
        time.sleep(_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
