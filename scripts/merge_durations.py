#!/usr/bin/env python3
"""Merge partial duration JSON files into .test_durations, dropping outliers.

Called by batch-store-durations.sh and store-backend-durations.sh:
    "$PYTHON" "$SCRIPT_DIR/merge_durations.py" "$SRUN_DIR" "$PWD/.test_durations"

Also importable by other scripts.

The merge serializes itself: see ``duration_artifact_lock`` below. Callers must
NOT wrap the invocation in an external locking binary -- `flock(1)` does not
exist on a Windows/Git-Bash developer host, and the wrapper that used to be in
store-backend-durations.sh failed the merge (exit 127) after the measurement
run had already succeeded.
"""

import contextlib
import errno
import glob
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from durations_quality import (
    IN_BAND_SETUP_GAP_MAX,
    IN_BAND_SETUP_MAX,
    IN_BAND_SETUP_MIN,
    MIN_CV,
    MIN_ENTRIES,
    MIN_UNIQUE,
    OUTLIER_CAP,
    in_band_setup_artifacts,
    quality_ok,
)
from testing.identity_extraction import extract_python_test_ids_from_file

# ── mutual exclusion for the shared duration artifact ────────────────────────
# Merging is a read-modify-write of ONE committed file (backend/.test_durations)
# and several producers reach it: scripts/store-backend-durations.sh (one
# process per targeted refresh), scripts/batch-store-durations.sh,
# scripts/regenerate-durations.py and scripts/merge_shard_durations.py. Without
# serialization, two merges that overlap both read the same baseline and the
# last writer silently discards the other's measurement.
#
# The serialization lives HERE, not in a shell wrapper around the call. The
# previous design wrapped the invocation in `flock(1)`, which does not exist in
# Git Bash on Windows: the merge step died with `flock: command not found`
# AFTER the tests had run, the caller's cleanup trap deleted the measurement
# directory, and the artifact was left untouched -- a silently lost run. A lock
# taken inside the merge covers every caller on Linux, macOS and Windows alike
# and needs no external binary.
#
# Scheme: an OS advisory lock on a sidecar file (`<artifact>.lock`), taken
# non-blocking in a bounded poll loop.
#   * POSIX  -> fcntl.flock(LOCK_EX | LOCK_NB)
#   * Windows -> msvcrt.locking(LK_NBLCK) over one byte; Windows permits
#     locking a region past EOF, so the sidecar stays zero-length.
#
# STALE-LOCK RECOVERY is a property of the scheme rather than a repair path:
# both primitives are owned by the OS and are released when the holding process
# exits, crash or kill included. So an abandoned lock cannot wedge future
# merges, and the sidecar FILE is deliberately never deleted -- it is only a
# handle to lock against, it is expected to already exist from a previous run,
# and unlinking it would let a second process create and lock a different inode
# under the same name while the first still believes it holds the artifact.
LOCK_TIMEOUT_ENV = "DURATION_MERGE_LOCK_TIMEOUT"
DEFAULT_LOCK_TIMEOUT_SECONDS = 300.0
_LOCK_POLL_SECONDS = 0.05

# Both primitives report contention through errno rather than a dedicated
# exception. POSIX flock uses EWOULDBLOCK (== EAGAIN); the Windows CRT reports
# EACCES; EDEADLK is what some POSIX kernels answer for a self-conflict.
_LOCK_CONTENTION_ERRNOS = frozenset(
    {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
)


class DurationLockTimeout(TimeoutError):
    """The duration-artifact lock stayed held past the caller's bounded wait.

    Raised rather than skipping the merge: a merge that is quietly dropped
    loses a measurement that may have cost twenty minutes of test time, which
    is the exact failure this lock exists to prevent.
    """


def lock_path_for(output_path: str | os.PathLike[str]) -> str:
    """Return the sidecar lock path guarding *output_path*."""
    return os.fspath(output_path) + ".lock"


def _configured_lock_timeout() -> float:
    raw = os.environ.get(LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    try:
        return max(float(raw), 0.0)
    except ValueError:
        raise ValueError(
            f"{LOCK_TIMEOUT_ENV} must be a number of seconds, got {raw!r}"
        ) from None


def _try_acquire(handle) -> bool:
    """One non-blocking attempt. True on success, False on contention."""
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _LOCK_CONTENTION_ERRNOS:
            return False
        raise
    return True


def _release(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def duration_artifact_lock(
    output_path: str | os.PathLike[str],
    timeout: float | None = None,
    poll_seconds: float = _LOCK_POLL_SECONDS,
):
    """Hold exclusive access to *output_path* for the body of the ``with``.

    ``timeout`` bounds the wait (default: ``DURATION_MERGE_LOCK_TIMEOUT`` or
    ``DEFAULT_LOCK_TIMEOUT_SECONDS``); exhausting it raises
    ``DurationLockTimeout`` naming the lock file, so a wedge reports itself
    instead of hanging a caller forever.
    """
    if timeout is None:
        timeout = _configured_lock_timeout()
    path = lock_path_for(output_path)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    deadline = time.monotonic() + max(timeout, 0.0)
    # "a+b" creates the sidecar when absent and NEVER truncates an existing
    # one, so a lock file left behind by an earlier run is reused as-is.
    handle = open(path, "a+b")
    try:
        while not _try_acquire(handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The path is interpolated verbatim rather than through !r:
                # repr() doubles every backslash in a Windows path, and a
                # caller (or a test) that greps the message for the lock it is
                # waiting on must find the path it actually passed.
                raise DurationLockTimeout(
                    f"timed out after {timeout:g}s waiting for the duration "
                    f"artifact lock {path} (guarding "
                    f"{os.fspath(output_path)}). Another duration merge is "
                    "still running; wait for it and retry. The lock is an OS "
                    "advisory lock, so a crashed holder releases it "
                    f"automatically -- set {LOCK_TIMEOUT_ENV} to wait longer."
                )
            time.sleep(min(poll_seconds, remaining))
        try:
            yield path
        finally:
            _release(handle)
    finally:
        handle.close()


def _valid_test_ids(path: Path) -> set[str]:
    """Return canonical IDs, treating unreadable source as no valid tests."""
    return extract_python_test_ids_from_file(path)


def filter_current_entries(data: dict, root: str | os.PathLike[str]) -> dict:
    """Drop entries whose test file or statically collectable ID is gone."""
    root_path = Path(root)
    valid_ids_by_file: dict[Path, set[str]] = {}
    current: dict = {}
    for key, value in data.items():
        file_key, separator, raw_node_id = key.partition("::")
        if not separator:
            continue
        test_path = root_path / file_key
        if not test_path.is_file():
            continue
        if test_path not in valid_ids_by_file:
            valid_ids_by_file[test_path] = _valid_test_ids(test_path)
        valid_ids = valid_ids_by_file[test_path]
        base_node_id = raw_node_id.split("[", 1)[0]
        if base_node_id not in valid_ids:
            continue
        current[key] = value
    return current


def load_duration_baseline(path: str) -> dict:
    """Load the existing artifact, retaining only entries in the current tree."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"duration baseline must be a JSON object: {path}")
    return filter_current_entries(data, os.path.dirname(os.path.abspath(path)))


def sanitize_fresh_entries(fresh: dict) -> tuple[dict, set[str]]:
    """Return the fresh observations with their own setup plateau removed.

    The baseline is deliberately kept out of this decision. Sweeping
    baseline-and-fresh together lets a historical observation inside the
    diagnostic gap vouch for a freshly recorded plateau, and the artifacts are
    then persisted as the new baseline -- where they are indistinguishable from
    real measurements. The fresh entries are the only ones a merge can still
    reject, so they are judged on their own evidence first. Callers still sweep
    the merged view afterwards, which is what strips a plateau that is already
    sitting in the baseline.
    """
    band_artifacts = in_band_setup_artifacts(fresh)
    return {
        key: value for key, value in fresh.items() if key not in band_artifacts
    }, band_artifacts


def merge(src_dir: str, output_path: str, lock_timeout: float | None = None) -> int:
    """Merge all *.json files from src_dir into output_path; return entry count.

    The read of the baseline and the rewrite of the artifact are one critical
    section: a concurrent merge that reads between them would compute its
    result from a stale baseline and overwrite this one's measurement.
    """
    with duration_artifact_lock(output_path, timeout=lock_timeout):
        return _merge_locked(src_dir, output_path)


def _merge_locked(src_dir: str, output_path: str) -> int:
    """merge()'s body. The caller must already hold the artifact lock."""
    root = os.path.dirname(os.path.abspath(output_path))
    merged = {}
    outlier_count = 0

    for k, v in load_duration_baseline(output_path).items():
        if v > OUTLIER_CAP:
            outlier_count += 1
        else:
            merged[k] = v

    # Fresh partial observations accumulate separately so the setup-plateau
    # sanitizer can judge them without the baseline vouching for them; `added`
    # still counts entries that are new to the baseline-and-partials view.
    fresh: dict = {}
    for path in sorted(glob.glob(os.path.join(src_dir, "*/*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = filter_current_entries(data, root)
        pre = len(merged.keys() | fresh.keys())
        for k, v in data.items():
            if v > OUTLIER_CAP:
                outlier_count += 1
                print(f"  SKIP outlier {v:.1f}s: {k}")
            else:
                fresh[k] = v
        added = len(merged.keys() | fresh.keys()) - pre
        print(f"  {os.path.relpath(path, src_dir)}: {len(data)} entries ({added} new)")

    fresh, fresh_band_artifacts = sanitize_fresh_entries(fresh)
    if fresh_band_artifacts:
        print(
            f"  Dropped {len(fresh_band_artifacts)} in-band setup artifacts from the "
            f"fresh observations ({IN_BAND_SETUP_MIN:g}-{IN_BAND_SETUP_MAX:g}s plateau; "
            f"no observations through {IN_BAND_SETUP_GAP_MAX:g}s)"
        )
    merged.update(fresh)

    band_artifacts = in_band_setup_artifacts(merged)
    for key in band_artifacts:
        del merged[key]
    if band_artifacts:
        print(
            f"  Dropped {len(band_artifacts)} in-band setup artifacts "
            f"({IN_BAND_SETUP_MIN:g}-{IN_BAND_SETUP_MAX:g}s plateau; "
            f"no observations through {IN_BAND_SETUP_GAP_MAX:g}s)"
        )

    if outlier_count:
        print(f"  Dropped {outlier_count} outlier(s) > {OUTLIER_CAP}s (fixture-setup misattribution)")

    # ``newline=""`` keeps the committed durations artifact LF-only when this
    # runs on a Windows host: platform translation would rewrite every one of
    # its thousands of lines as CRLF and produce a whole-file diff.
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")

    vs = list(merged.values())
    n = len(vs)
    uv = len(set(vs))
    avg = statistics.mean(vs) if vs else 0.0
    sd = statistics.stdev(vs) if n > 1 else 0.0
    cv = sd / avg if avg > 0 else 0.0

    print()
    print(f"Total entries: {n}")
    print(f"Unique values: {uv}")
    print(f"Mean: {avg:.4f}s | SD: {sd:.4f}s | CV: {cv:.4f}")
    if vs:
        print(f"Min: {min(vs):.4f}s | Max: {max(vs):.4f}s")
    ok = quality_ok(n, uv, cv)
    print(f"Guard check: {'PASS' if ok else 'FAIL'} (needs n>={MIN_ENTRIES}, uv>={MIN_UNIQUE}, CV>{MIN_CV})")

    if not ok:
        if n < MIN_ENTRIES:
            print(f"  FAIL: entries: {n} < {MIN_ENTRIES}")
        if uv < MIN_UNIQUE:
            print(f"  FAIL: unique values: {uv} < {MIN_UNIQUE}")
        if cv <= MIN_CV:
            print(f"  FAIL: CV > {MIN_CV}: {cv:.4f}")

    return n


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <src_dir> <output_path>", file=sys.stderr)
        sys.exit(1)
    try:
        count = merge(sys.argv[1], sys.argv[2])
    except DurationLockTimeout as exc:
        # Distinct exit code: "another merge held the artifact" is a retryable
        # condition, not the "merged nothing" verdict below, and a caller that
        # kept its raw measurement can retry the merge alone.
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if count > 0 else 1)
