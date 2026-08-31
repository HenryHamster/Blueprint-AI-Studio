#!/usr/bin/env python3
"""Apply a fail-closed Landlock filesystem allowlist, then exec one command.

This wrapper runs *inside* the runner's existing outer bwrap namespace.  It does
not create another namespace and it does not proxy network traffic.  The child
inherits only the filesystem access explicitly granted below; the policy also
probes its deny and writable roots after restriction and before exec.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import platform
import resource
import stat
import sys
from typing import Iterable, NoReturn


EXIT_POLICY = 126
MAX_POLICY_BYTES = 64 * 1024
MAX_PATHS = 96
MIN_LANDLOCK_ABI = 5
MAX_LANDLOCK_ABI = 8

LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REMOVE_DIR = 1 << 4
ACCESS_REMOVE_FILE = 1 << 5
ACCESS_MAKE_CHAR = 1 << 6
ACCESS_MAKE_DIR = 1 << 7
ACCESS_MAKE_REG = 1 << 8
ACCESS_MAKE_SOCK = 1 << 9
ACCESS_MAKE_FIFO = 1 << 10
ACCESS_MAKE_BLOCK = 1 << 11
ACCESS_MAKE_SYM = 1 << 12
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14
ACCESS_IOCTL_DEV = 1 << 15

ACCESS_ABI_1 = (
    ACCESS_EXECUTE
    | ACCESS_WRITE_FILE
    | ACCESS_READ_FILE
    | ACCESS_READ_DIR
    | ACCESS_REMOVE_DIR
    | ACCESS_REMOVE_FILE
    | ACCESS_MAKE_CHAR
    | ACCESS_MAKE_DIR
    | ACCESS_MAKE_REG
    | ACCESS_MAKE_SOCK
    | ACCESS_MAKE_FIFO
    | ACCESS_MAKE_BLOCK
    | ACCESS_MAKE_SYM
)
ACCESS_READ_ONLY = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
ACCESS_FILE_BASE = ACCESS_EXECUTE | ACCESS_WRITE_FILE | ACCESS_READ_FILE
ACCESS_SCRATCH = (
    ACCESS_WRITE_FILE
    | ACCESS_READ_FILE
    | ACCESS_READ_DIR
    | ACCESS_REMOVE_DIR
    | ACCESS_REMOVE_FILE
    | ACCESS_MAKE_DIR
    | ACCESS_MAKE_REG
    | ACCESS_MAKE_SOCK
    | ACCESS_MAKE_FIFO
    | ACCESS_MAKE_SYM
    | ACCESS_REFER
    | ACCESS_TRUNCATE
)


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int),
    ]


def fail(message: str) -> NoReturn:
    print(f"resident_landlock: {message}", file=sys.stderr)
    raise SystemExit(EXIT_POLICY)


def syscall_numbers() -> tuple[int, int, int]:
    if platform.system() != "Linux":
        fail("Linux is required")
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        fail(f"unsupported Linux architecture: {machine or 'unknown'}")
    return 444, 445, 446


def checked_syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    ctypes.set_errno(0)
    result = int(libc.syscall(number, *args))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def handled_access_for_abi(abi: int) -> int:
    # ABI 3 is the first version that mediates truncate/ftruncate.  Older
    # kernels would make a nominally read-only credential or repository file
    # destructively writable, so there is no degraded fallback.
    if abi < MIN_LANDLOCK_ABI or abi > MAX_LANDLOCK_ABI:
        fail(f"unsupported Landlock ABI: {abi}")
    access = ACCESS_ABI_1
    if abi >= 2:
        access |= ACCESS_REFER
    if abi >= 3:
        access |= ACCESS_TRUNCATE
    if abi >= 5:
        access |= ACCESS_IOCTL_DEV
    return access


def normalized_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        fail(f"{label} contains an invalid path")
    if not os.path.isabs(value):
        fail(f"{label} path is not absolute")
    resolved = os.path.realpath(value)
    if resolved == "/":
        fail(f"{label} cannot grant the filesystem root")
    if not os.path.exists(resolved):
        fail(f"{label} path does not exist: {resolved}")
    return resolved


def validate_private_writable(path: str) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        fail(f"readwrite_paths must contain exactly one private directory: {path}")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        fail(f"readwrite_paths must be owned by the current uid with mode 0700: {path}")
    if (
        path in {"/tmp", "/workspace", "/home"}
        or path.startswith("/home/") and path.count("/") < 3
    ):
        fail(f"readwrite path is not a private Resident scratch root: {path}")


def path_list(raw: object, label: str) -> list[str]:
    if not isinstance(raw, list) or len(raw) > MAX_PATHS:
        fail(f"{label} must be a bounded path list")
    result: list[str] = []
    for value in raw:
        resolved = normalized_path(value, label)
        if resolved not in result:
            result.append(resolved)
    return result


def is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def validate_separation(
    readonly: Iterable[str],
    readwrite: Iterable[str],
    deny_read: Iterable[str],
    deny_write: Iterable[str],
) -> None:
    for readonly_path in readonly:
        for readwrite_path in readwrite:
            if is_within(readonly_path, readwrite_path) or is_within(readwrite_path, readonly_path):
                fail(f"readonly/readwrite roots overlap: {readonly_path} <> {readwrite_path}")
    for allowed_path in [*readonly, *readwrite]:
        for denied_path in deny_read:
            if is_within(allowed_path, denied_path) or is_within(denied_path, allowed_path):
                fail(f"read allow/deny roots overlap: {allowed_path} <> {denied_path}")
    for readwrite_path in readwrite:
        for denied_path in deny_write:
            if is_within(readwrite_path, denied_path) or is_within(denied_path, readwrite_path):
                fail(f"write allow/deny roots overlap: {readwrite_path} <> {denied_path}")


def open_stable_path(path: str) -> tuple[int, os.stat_result]:
    before = os.lstat(path)
    flags = os.O_PATH | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.path.realpath(f"/proc/self/fd/{fd}")
        if opened != path:
            fail(f"path changed while opening: {path}")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            fail(f"path identity changed while opening: {path}")
        if not (stat.S_ISDIR(after.st_mode) or stat.S_ISREG(after.st_mode) or stat.S_ISCHR(after.st_mode)):
            fail(f"unsupported policy path type: {path}")
        return fd, after
    except BaseException:
        os.close(fd)
        raise


def rights_for_stat(requested: int, info: os.stat_result, abi: int) -> int:
    if stat.S_ISDIR(info.st_mode):
        return requested
    allowed = ACCESS_FILE_BASE
    if abi >= 3:
        allowed |= ACCESS_TRUNCATE
    if abi >= 5 and (stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)):
        allowed |= ACCESS_IOCTL_DEV
    return requested & allowed


def add_path_rule(
    libc: ctypes.CDLL,
    add_rule_number: int,
    ruleset_fd: int,
    path: str,
    requested: int,
    abi: int,
) -> None:
    fd, info = open_stable_path(path)
    try:
        allowed = rights_for_stat(requested, info, abi)
        if allowed == 0:
            fail(f"path receives no applicable Landlock rights: {path}")
        attr = PathBeneathAttr(allowed_access=allowed, parent_fd=fd)
        checked_syscall(
            libc,
            add_rule_number,
            ruleset_fd,
            LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
    finally:
        os.close(fd)


def denied_read_probe(path: str) -> None:
    try:
        info = os.stat(path, follow_symlinks=True)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                next(entries, None)
        else:
            with open(path, "rb") as handle:
                handle.read(1)
    except PermissionError:
        return
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM}:
            return
        fail(f"read-denial probe errored for {path}: errno={error.errno}")
    fail(f"read-denial probe unexpectedly succeeded: {path}")


def denied_write_probe(path: str) -> None:
    if not os.path.isdir(path):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
        except PermissionError:
            pass
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                fail(f"write-denial probe errored for {path}: errno={error.errno}")
        else:
            os.close(fd)
            fail(f"write-denial probe unexpectedly opened file: {path}")
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    probe = os.path.join(directory, f".resident-landlock-deny-{os.getpid()}")
    fd: int | None = None
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except PermissionError:
        return
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            return
        fail(f"write-denial probe errored for {path}: errno={error.errno}")
    finally:
        if fd is not None:
            os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        pass
    fail(f"write-denial probe unexpectedly succeeded: {path}")


def writable_probe(path: str) -> None:
    probe = os.path.join(path, f".resident-landlock-write-{os.getpid()}")
    renamed = f"{probe}.renamed"
    child_dir = f"{probe}.dir"
    child_link = f"{probe}.link"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.write(fd, b"resident-landlock")
        os.ftruncate(fd, 8)
        os.close(fd)
        os.rename(probe, renamed)
        os.mkdir(child_dir, 0o700)
        os.symlink(os.path.basename(renamed), child_link)
        os.unlink(child_link)
        os.unlink(renamed)
        os.rmdir(child_dir)
    except OSError as error:
        fail(f"writable probe failed for {path}: errno={error.errno}")


def exact_executable(command: list[str]) -> str:
    candidate = command[0]
    if not os.path.isabs(candidate):
        fail("command executable must be absolute")
    resolved = os.path.realpath(candidate)
    if candidate != resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        fail("command executable must be canonical, regular, and executable")
    return resolved


def close_inherited_fds() -> None:
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    ceiling = 1_048_576 if soft_limit == resource.RLIM_INFINITY else int(soft_limit)
    try:
        os.closerange(3, max(3, ceiling))
    except OSError as error:
        fail(f"inherited file descriptor closure failed: errno={error.errno}")


def validate_stdio() -> None:
    for fd in range(3):
        try:
            info = os.fstat(fd)
        except OSError as error:
            fail(f"stdio descriptor {fd} is unavailable: errno={error.errno}")
        if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
            fail(f"stdio descriptor {fd} has an unsafe filesystem capability")


def child_environment() -> dict[str, str]:
    allowed = {
        "HOME", "TMPDIR", "CODEX_HOME", "PATH", "LANG", "LC_ALL", "TERM",
        "OPENAI_API_KEY", "CODEX_API_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def parse_policy(raw: str) -> tuple[list[str], list[str], list[str], list[str]]:
    if len(raw.encode("utf-8")) > MAX_POLICY_BYTES or "\0" in raw:
        fail("policy JSON is oversized or contains NUL")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fail("policy JSON is invalid")
    if not isinstance(parsed, dict) or set(parsed) != {
        "version", "readonly_paths", "readwrite_paths", "deny_read_paths", "deny_write_paths"
    }:
        fail("policy JSON has an unsupported shape")
    if parsed["version"] != 1:
        fail("policy JSON version is unsupported")
    readonly = path_list(parsed["readonly_paths"], "readonly_paths")
    readwrite = path_list(parsed["readwrite_paths"], "readwrite_paths")
    deny_read = path_list(parsed["deny_read_paths"], "deny_read_paths")
    deny_write = path_list(parsed["deny_write_paths"], "deny_write_paths")
    if not readonly or not readwrite or not deny_read or not deny_write:
        fail("policy JSON must include all four non-empty path sets")
    return readonly, readwrite, deny_read, deny_write


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--policy-json", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    if not command or any("\0" in arg for arg in command):
        fail("an exact command is required")

    readonly, readwrite, deny_read, deny_write = parse_policy(options.policy_json)
    if len(readwrite) != 1:
        fail("policy must contain exactly one private writable scratch root")
    validate_private_writable(readwrite[0])
    command[0] = exact_executable(command)
    validate_separation(readonly, readwrite, deny_read, deny_write)
    validate_stdio()
    close_inherited_fds()
    os.chdir(readwrite[0])
    os.umask(0o077)

    if ctypes.sizeof(RulesetAttr) != 8 or ctypes.sizeof(PathBeneathAttr) != 12:
        fail("Landlock ctypes structure layout is unsupported")

    create_number, add_number, restrict_number = syscall_numbers()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    try:
        abi = checked_syscall(
            libc,
            create_number,
            0,
            0,
            LANDLOCK_CREATE_RULESET_VERSION,
        )
    except OSError as error:
        fail(f"Landlock ABI query failed: errno={error.errno}")
    handled = handled_access_for_abi(abi)
    ruleset_attr = RulesetAttr(handled_access_fs=handled)
    try:
        ruleset_fd = checked_syscall(
            libc,
            create_number,
            ctypes.byref(ruleset_attr),
            ctypes.sizeof(ruleset_attr),
            0,
        )
    except OSError as error:
        fail(f"Landlock ruleset creation failed: errno={error.errno}")
    try:
        for path in readonly:
            add_path_rule(libc, add_number, ruleset_fd, path, ACCESS_READ_ONLY, abi)
        for path in readwrite:
            add_path_rule(libc, add_number, ruleset_fd, path, ACCESS_SCRATCH, abi)
        # Node implements child stdio='ignore' with this exact device, and the
        # generated strategic helpers also use it for bounded redirection. Keep
        # the exception fixed and device-specific rather than granting /dev.
        add_path_rule(
            libc,
            add_number,
            ruleset_fd,
            "/dev/null",
            ACCESS_READ_FILE | ACCESS_WRITE_FILE,
            abi,
        )
        ctypes.set_errno(0)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            code = ctypes.get_errno()
            fail(f"PR_SET_NO_NEW_PRIVS failed: errno={code}")
        try:
            checked_syscall(libc, restrict_number, ruleset_fd, 0)
        except OSError as error:
            fail(f"Landlock restriction failed: errno={error.errno}")
    finally:
        os.close(ruleset_fd)

    for path in deny_read:
        denied_read_probe(path)
    for path in deny_write:
        denied_write_probe(path)
    for path in readwrite:
        writable_probe(path)

    child_env = child_environment()
    child_env["BLUEPRINT_RESIDENT_LANDLOCK"] = f"v1:abi{abi}"
    try:
        os.execve(command[0], command, child_env)
    except OSError as error:
        fail(f"restricted command exec failed: errno={error.errno}")
    return 127


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as error:
        code = error.errno if isinstance(error, OSError) else None
        fail(f"policy setup failed{f': errno={code}' if code is not None else ''}")
