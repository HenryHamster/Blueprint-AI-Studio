#!/usr/bin/env python3
"""Transactional exact-SHA replacement for a self-hosted runner checkout.

The authenticated heartbeat owns the desired commit identity; ``origin`` owns
the corresponding Git object. This module joins those authorities without
moving a local branch pointer. It intentionally supports divergent recovery
commits and rollback by checking out the origin-proven commit detached.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from typing import Any

try:  # Linux production path.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows hosts.
    fcntl = None  # type: ignore[assignment]

try:  # Windows test/developer path.
    import msvcrt
except ImportError:  # pragma: no cover - exercised on Linux hosts.
    msvcrt = None  # type: ignore[assignment]


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_Run = Callable[..., Any]


class _UpdateError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise _UpdateError(message)


def _command(run: _Run, argv: list[str], *, cwd: str, timeout: int = 30):
    try:
        return run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail(f"command failed to start ({argv[0]}): {exc}")


def _git(run: _Run, repo: str, *args: str):
    return _command(run, ["git", *args], cwd=repo)


def _worktree_transition_path(run: _Run, repo: str) -> str:
    """Return one journal path shared by every runner using this worktree."""
    result = _git(run, repo, "rev-parse", "--absolute-git-dir")
    if result.returncode:
        _fail("cannot resolve the runner worktree Git directory")
    git_dir = os.path.realpath(result.stdout.strip())
    if not os.path.isabs(git_dir) or not os.path.isdir(git_dir):
        _fail("runner worktree Git directory is invalid")
    return os.path.join(
        git_dir,
        "blueprint-runner-update",
        "transition.json",
    )


def _secure_regular_file(path: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        _fail(f"update state path is not a regular file: {path}")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            _fail(f"update state file is not owned by this runner: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            _fail(f"update state file permissions are too broad: {path}")


def _fsync_directory(directory: str) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_state_directory(path: str) -> str:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.name != "nt":
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            _fail(f"update state directory is not runner-owned: {directory}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            _fail(f"update state directory permissions are too broad: {directory}")
    return directory


@contextlib.contextmanager
def _exclusive_lock(path: str) -> Iterator[None]:
    directory = _ensure_state_directory(path)
    lock_path = f"{path}.lock"
    _secure_regular_file(lock_path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        _fsync_directory(directory)
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail("another runner update process owns the checkout lock")
        elif msvcrt is not None:  # pragma: no cover - platform-specific.
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                _fail("another runner update process owns the checkout lock")
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - platform-specific.
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def _read_marker(path: str) -> dict[str, Any] | None:
    _secure_regular_file(path)
    try:
        with open(path, encoding="utf-8") as handle:
            marker = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read pending update state: {exc}")
    if not isinstance(marker, dict):
        _fail("pending update state is not an object")
    return marker


def _write_marker(path: str, marker: dict[str, Any]) -> None:
    directory = _ensure_state_directory(path)
    fd, temporary = tempfile.mkstemp(prefix=".runner-update-", dir=directory)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        _fsync_directory(directory)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _clear_marker(path: str) -> None:
    directory = os.path.dirname(path) or "."
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _fsync_directory(directory)


def _safe_branch(value: object) -> bool:
    return (
        isinstance(value, str)
        and _BRANCH_RE.fullmatch(value) is not None
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and ".." not in value
        and "@{" not in value
        and not value.endswith((".", ".lock"))
    )


def _identity(run: _Run, repo: str) -> tuple[str, str]:
    head = _git(run, repo, "rev-parse", "HEAD")
    branch = _git(run, repo, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode or branch.returncode:
        _fail("cannot resolve checkout identity")
    head_sha = head.stdout.strip()
    branch_name = branch.stdout.strip()
    if _SHA_RE.fullmatch(head_sha) is None:
        _fail("checkout HEAD is not a full commit identity")
    if branch_name != "HEAD" and not _safe_branch(branch_name):
        _fail("checkout branch cannot be restored safely")
    return head_sha, branch_name


def _require_clean(run: _Run, repo: str) -> None:
    result = _git(run, repo, "status", "--porcelain=v1", "--untracked-files=normal")
    if result.returncode or result.stdout:
        _fail("exact update requires a clean worktree")


def _fetch_verified(run: _Run, repo: str, commit: str) -> None:
    if _SHA_RE.fullmatch(commit) is None:
        _fail("exact update target must be 40 lowercase hex characters")
    fetched = _git(run, repo, "fetch", "--no-tags", "origin", commit)
    verified = _git(run, repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
    if fetched.returncode or verified.returncode or verified.stdout.strip() != commit:
        _fail("origin did not verify the exact requested commit")


def _branch_points_to(run: _Run, repo: str, branch: str, commit: str) -> bool:
    result = _git(
        run, repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
    )
    return result.returncode == 0 and result.stdout.strip() == commit


def _lock_dirs(run: _Run, repo: str, old: str, target: str) -> list[str]:
    result = _git(run, repo, "diff", "--name-only", "-z", f"{old}..{target}", "--")
    if result.returncode:
        _fail("cannot inspect exact-target dependency changes")
    root = os.path.realpath(repo)
    found: list[str] = []
    for relative in result.stdout.split("\0"):
        normalized = relative.replace("\\", "/")
        if not relative or not (
            normalized == "package-lock.json"
            or normalized.endswith("/package-lock.json")
        ):
            continue
        if os.path.isabs(relative) or ".." in normalized.split("/"):
            _fail("exact target contains an unsafe package-lock path")
        directory = os.path.abspath(
            os.path.dirname(os.path.join(root, *normalized.split("/")))
        )
        if os.path.commonpath([root, directory]) != root:
            _fail("exact target package-lock path escapes the checkout")
        if directory not in found:
            found.append(directory)
    return found


def _install_dependencies(run: _Run, repo: str, directories: list[str]) -> None:
    root = os.path.realpath(repo)
    for lexical_dir in directories:
        real_dir = os.path.realpath(lexical_dir)
        if os.path.commonpath([root, real_dir]) != root:
            _fail("package-lock directory resolves outside the checkout")
        if not os.path.isfile(os.path.join(real_dir, "package-lock.json")):
            continue
        result = _command(run, ["npm", "ci"], cwd=real_dir, timeout=120)
        if result.returncode:
            _fail(f"npm ci failed in {real_dir} (exit={result.returncode})")


def _validate_marker(marker: dict[str, Any], repo: str) -> None:
    target = marker.get("target_commit")
    old_head = marker.get("old_head")
    if (
        marker.get("repo_root") != os.path.realpath(repo)
        or not isinstance(target, str)
        or _SHA_RE.fullmatch(target) is None
        or not isinstance(old_head, str)
        or _SHA_RE.fullmatch(old_head) is None
        or marker.get("phase") not in {"target_pending", "rollback_pending"}
        or (
            marker.get("old_branch") is not None
            and not _safe_branch(marker.get("old_branch"))
        )
    ):
        _fail("pending update marker failed validation")


def _postcheck(
    run: _Run,
    repo: str,
    *,
    head: str,
    branch: str,
    preserved_branch: str | None = None,
    preserved_head: str | None = None,
) -> None:
    if _identity(run, repo) != (head, branch):
        _fail("checkout identity verification failed")
    if preserved_branch and not _branch_points_to(
        run, repo, preserved_branch, str(preserved_head)
    ):
        _fail("exact update moved the original branch pointer")
    _require_clean(run, repo)


def _rollback(
    run: _Run,
    repo: str,
    marker: dict[str, Any],
    directories: list[str],
    transition_path: str,
) -> None:
    old = marker["old_head"]
    branch = marker["old_branch"]
    current, _ = _identity(run, repo)
    if current not in {old, marker["target_commit"]}:
        _fail("checkout moved outside the pending transition; refusing rollback")
    if branch is not None:
        if not _branch_points_to(run, repo, branch, old):
            _fail("original branch moved during exact update")
        checkout = _git(run, repo, "checkout", "--quiet", branch)
        expected_branch = branch
    else:
        checkout = _git(run, repo, "checkout", "--quiet", "--detach", old)
        expected_branch = "HEAD"
    if checkout.returncode:
        _fail("could not restore original checkout identity")
    _install_dependencies(run, repo, directories)
    _postcheck(run, repo, head=old, branch=expected_branch)
    _clear_marker(transition_path)


def _mark_rollback(
    marker: dict[str, Any], transition_path: str
) -> dict[str, Any]:
    marker = dict(marker)
    marker["phase"] = "rollback_pending"
    _write_marker(transition_path, marker)
    return marker


def _update_locked(
    *, repo: str, target: str, transition_path: str, run: _Run
) -> bool:
    _fetch_verified(run, repo, target)
    _require_clean(run, repo)
    current_head, current_branch = _identity(run, repo)
    marker = _read_marker(transition_path)

    if marker is not None:
        _validate_marker(marker, repo)
        prior_target = marker["target_commit"]
        if prior_target != target:
            _fetch_verified(run, repo, prior_target)
            prior_dirs = _lock_dirs(run, repo, marker["old_head"], prior_target)
            marker = _mark_rollback(marker, transition_path)
            _rollback(run, repo, marker, prior_dirs, transition_path)
            marker = None
            current_head, current_branch = _identity(run, repo)

    if marker is None and current_head == target and current_branch == "HEAD":
        _postcheck(run, repo, head=target, branch="HEAD")
        return True

    if marker is None:
        old_branch = None if current_branch == "HEAD" else current_branch
        marker = {
            "repo_root": os.path.realpath(repo),
            "target_commit": target,
            "old_head": current_head,
            "old_branch": old_branch,
            "phase": "target_pending",
        }
        _write_marker(transition_path, marker)
    else:
        current_head, current_branch = _identity(run, repo)

    old = marker["old_head"]
    branch = marker["old_branch"]
    directories = _lock_dirs(run, repo, old, target)
    if marker["phase"] == "rollback_pending":
        _rollback(run, repo, marker, directories, transition_path)
        return False

    if (current_head, current_branch) == (target, "HEAD"):
        pass  # Resume a crash after checkout but before dependency commit.
    elif (current_head, current_branch) == (old, branch or "HEAD"):
        if branch is not None and not _branch_points_to(run, repo, branch, old):
            _fail("original branch moved before exact update")
        checkout = _git(run, repo, "checkout", "--quiet", "--detach", target)
        if checkout.returncode:
            marker = _mark_rollback(marker, transition_path)
            _rollback(run, repo, marker, directories, transition_path)
            return False
    else:
        _fail("checkout identity does not match the pending transition")

    try:
        _postcheck(
            run,
            repo,
            head=target,
            branch="HEAD",
            preserved_branch=branch,
            preserved_head=old,
        )
        _install_dependencies(run, repo, directories)
        _postcheck(
            run,
            repo,
            head=target,
            branch="HEAD",
            preserved_branch=branch,
            preserved_head=old,
        )
        _clear_marker(transition_path)
        return True
    except _UpdateError:
        marker = _mark_rollback(marker, transition_path)
        _rollback(run, repo, marker, directories, transition_path)
        return False


def update_to_exact_commit(
    *,
    repo_root: str,
    target_commit: str,
    transition_path: str | None = None,
    run: _Run = subprocess.run,
) -> bool:
    """Apply an origin-proven exact commit transactionally; never raise."""
    if not isinstance(target_commit, str) or _SHA_RE.fullmatch(target_commit) is None:
        print(
            "ERROR: exact runner update failed: exact update target must be "
            "40 lowercase hex characters",
            flush=True,
        )
        return False
    repo = os.path.realpath(repo_root)
    try:
        transition_path = transition_path or _worktree_transition_path(run, repo)
        with _exclusive_lock(transition_path):
            return _update_locked(
                repo=repo,
                target=target_commit,
                transition_path=transition_path,
                run=run,
            )
    except (_UpdateError, OSError) as exc:
        print(f"ERROR: exact runner update failed: {exc}", flush=True)
        return False
