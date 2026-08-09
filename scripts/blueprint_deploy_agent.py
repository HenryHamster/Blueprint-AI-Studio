#!/usr/bin/env python3
"""Host-side deploy agent for approved in-product activations.

A durable long-running poller for the trusted control-plane host.  It watches
the backend's approved-activation contract and performs the already-existing
root-owned shim activation when an operator has approved a promoted version —
no hand-run CLI command, no SSH, and no new privilege.

The split keeps the privilege boundary where it already is: "approve" is an
in-product decision (``POST .../approve-activation``), "execute" is the
privileged step that must stay on the host.  This agent is the ONLY new
capability gained, and it is bounded by the same root-owned shim
(``/usr/local/sbin/blueprint-release``, installed from ``blueprint_release.py``
and allowed via the scoped sudoers rule) that ``blueprint deploy`` already
uses — the API/worker containers remain unable to touch host root.

Agent loop (one poll cycle):

1. ``GET /api/admin/projects/{project_id}/release-versions?status=promoted&limit=1``
   — the same endpoint ``cmd_deploy`` already reads.
2. Treat the latest promoted version as a PENDING approved binding when
   ``activation_requested_at`` is set and ``serving_status`` is still
   ``unconfirmed`` (awaiting execution) — the exact predicate the backend's
   approve-activation endpoint requires before it will stamp an approval.
3. Resolve the approved artifact EXACTLY: find the CI deploy run for the
   version's immutable ``promoted_sha``, download + checksum-verify its
   manifest, and refuse unless the manifest's built revision EQUALS that
   ``promoted_sha``.  A descendant or unrelated revision is NOT the approved
   artifact and is never executed — the approval can never authorize a
   different or historical artifact (mirroring the backend receipt guards in
   ``release_activation.py``).
4. Run ``sudo /usr/local/sbin/blueprint-release <digest> <revision>`` as a
   local subprocess (the agent runs ON the control-plane host) — reusing the
   canonical CI-artifact validation, promoted-SHA ancestry binding, root-owned
   shim, and rollback behavior from ``blueprint.py`` via shared helpers.
5. On success, POST the activation receipt (``.../activation-receipt``) so the
   backend records the served revision and resolves serving confirmation.

Conservative failure posture: a missing/stale/superseded approval, unverified
ancestry, credential/API error, or release failure WAITS or REPORTS and never
falsely confirms serving.  A stale approval that a newer promotion supersedes
is refused before the shim runs; a receipt rejected by the backend (which
independently enforces the approval binding) is logged and retried on the next
cycle.

Serialization: an ``flock`` on a lockfile ensures at most one deploy executes
at a time across agent restarts.  Transient control-plane failures are logged
and retried at the poll cadence with a backoff multiplier after a failure.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# The agent reuses the operator CLI's validated deploy chain so the canonical
# CI-artifact validation, promoted-SHA ancestry binding, root-owned shim
# invocation, and activation-receipt semantics are defined exactly once.
try:
    from blueprint import (
        APIError,
        _execute_deploy_shim,
        _post_activation_receipt,
        _resolve_deploy_binding,
        _resolve_deploy_manifest,
        _safe_read,
        get_project_id,
    )
except ImportError:  # pragma: no cover - installed layout puts scripts/ on path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blueprint import (  # noqa: E402
        APIError,
        _execute_deploy_shim,
        _post_activation_receipt,
        _resolve_deploy_binding,
        _resolve_deploy_manifest,
        _safe_read,
        get_project_id,
    )

_POLL_INTERVAL_SECONDS = 30
_BACKOFF_MULTIPLIER = 10
# User-writable default under the operator's home — the same ~/.blueprint state
# dir blueprint_runner uses — so the documented systemd unit (which runs as the
# unprivileged operator account with no RuntimeDirectory) can create the
# lockfile out of the box.  /run is root-owned 0755; a default there would make
# the agent dead-on-arrival (open() EACCES every cycle).  Overridable with
# --lockfile.
_DEFAULT_LOCKFILE = "~/.blueprint/deploy-agent.lock"
_DEFAULT_STATE_PATH = "~/.blueprint/deploy-agent.state"
_DEFAULT_SHIM = "/usr/local/sbin/blueprint-release"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

logger = logging.getLogger("blueprint-deploy-agent")


class _TransientError(RuntimeError):
    """A control-plane failure that should be retried, not fatal."""


def _latest_promoted_version(project_id: str) -> dict | None:
    """Return the project's latest promoted version, or ``None`` when absent.

    Uses ``_safe_read`` so a credential/API failure degrades to ``None`` and a
    short message instead of raising; the caller treats that as "wait and
    re-poll" (conservative — never guess a binding from a failed read).
    """
    data, err = _safe_read(
        f"/api/admin/projects/{project_id}/release-versions?status=promoted&limit=1"
    )
    if err:
        raise _TransientError(f"could not read promoted release versions: {err}")
    versions = (data or {}).get("versions") or []
    return versions[0] if versions else None


def _pending_approved_binding(version: dict | None) -> dict | None:
    """Return ``version`` when it is a pending approved activation, else ``None``.

    A pending approved binding has an operator approval
    (``activation_requested_at`` set) and is still ``unconfirmed`` — exactly the
    predicate ``POST /approve-activation`` requires before it stamps an
    approval, so the agent executes only bindings the backend could have
    created.  Resolved versions (``serving_confirmed`` / ``deploy_failed`` /
    ``confirmation_timed_out``), a never-armed ``NULL`` serving status, and
    never-approved versions are all skipped.  Only the CURRENT promoted version
    is ever polled, so a superseded approval on an older version is structurally
    never reached; the backend receipt guards independently reject a race.
    """
    if version is None:
        return None
    if version.get("activation_requested_at") is None:
        return None
    if version.get("serving_status") != "unconfirmed":
        return None
    return version


def _deploy_args(agent_args: argparse.Namespace, binding_sha: str) -> SimpleNamespace:
    """Build the shared-deploy-chain argument namespace for the agent.

    The agent executes the CURRENT approved binding: ``sha`` pinned to the
    approved version's immutable ``promoted_sha``, no ``--force``, no prompt,
    no SSH (the local ``inplace`` shim path).  ``host`` is the local hostname
    for receipt provenance.
    """
    return SimpleNamespace(
        sha=binding_sha,
        project_id=agent_args.project_id,
        host=agent_args.host,
        force=False,
        dry_run=False,
        yes=True,
    )


def _execute_pending_activation(agent_args: argparse.Namespace) -> int:
    """Execute the current approved binding, or report conservatively.

    Returns 0 when nothing was pending or the activation completed; returns 1
    (no receipt, no false confirmation) when the binding could not be resolved
    or was refused.  Raises only for unexpected internal errors; control-plane
    failures are logged and returned so the loop can back off.
    """
    from blueprint import load_config  # noqa: PLC0415

    if load_config().get("read_only"):
        logger.error(
            "read-only Blueprint credential cannot execute an approved activation"
        )
        return 1
    project_id = get_project_id(agent_args.project_id)
    try:
        version = _latest_promoted_version(project_id)
    except _TransientError as exc:
        logger.warning("skipping cycle: %s", exc)
        return 1
    version = _pending_approved_binding(version)
    if version is None:
        logger.info("no pending approved activation")
        return 0

    binding_sha = (version.get("promoted_sha") or "").lower()
    version_number = version.get("version_number")
    if not _SHA40_RE.fullmatch(binding_sha):
        logger.error(
            "REFUSING: approved version v%s has no valid promoted SHA (%r); "
            "not activating",
            version_number,
            (version.get("promoted_sha") or "")[:12],
        )
        return 1
    logger.info(
        "pending approved activation: v%s (promoted %s…)",
        version_number,
        binding_sha[:12],
    )

    deploy_args = _deploy_args(agent_args, binding_sha)
    try:
        revision, digest, _run = _resolve_deploy_manifest(deploy_args)
    except APIError as exc:
        logger.warning(
            "could not resolve the approved CI artifact for %s; waiting "
            "conservatively: %s",
            binding_sha[:12],
            exc,
        )
        return 1
    revision = revision.lower()

    if revision != binding_sha:
        logger.error(
            "REFUSING: approved binding is %s but the CI artifact for that "
            "promotion is %s; not activating a different artifact",
            binding_sha[:12],
            revision[:12],
        )
        return 1

    try:
        promoted = _resolve_deploy_binding(deploy_args, revision)
    except APIError as exc:
        logger.warning("binding check refused the revision; waiting: %s", exc)
        return 1
    if promoted is None:
        logger.error("binding check found no promoted version; not activating")
        return 1
    if str(promoted.get("id")) != str(version.get("id")):
        logger.error(
            "REFUSING: the approved version was superseded since polling; "
            "not activating a historical artifact",
        )
        return 1

    if agent_args.dry_run:
        logger.info("dry run: would activate v%s (%s)", version_number, revision[:12])
        return 0

    state_path = Path(
        getattr(agent_args, "state_path", _DEFAULT_STATE_PATH)
    ).expanduser()
    pending_receipt = _read_activation_state(state_path)
    if pending_receipt == {
        "version_number": version_number,
        "promoted_sha": binding_sha,
        "revision": revision,
    }:
        try:
            _post_activation_receipt(deploy_args, promoted, revision)
        except APIError as exc:
            logger.error(
                "release activated, but the activation receipt could not be recorded: %s",
                exc,
            )
            return 1
        _clear_activation_state(state_path)
        logger.info("activation receipt recorded for v%s", version_number)
        return 0

    try:
        _execute_deploy_shim(deploy_args, revision, digest, inplace=True)
    except APIError as exc:
        logger.error("activation failed; no receipt posted: %s", exc)
        return 1
    _write_activation_state(
        state_path,
        {
            "version_number": version_number,
            "promoted_sha": binding_sha,
            "revision": revision,
        },
    )
    try:
        _post_activation_receipt(deploy_args, promoted, revision)
    except APIError as exc:
        logger.error(
            "release activated, but the activation receipt could not be recorded: %s",
            exc,
        )
        return 1
    _clear_activation_state(state_path)
    logger.info("activation complete for v%s (%s)", version_number, revision[:12])
    return 0


def _read_activation_state(path: Path) -> dict | None:
    """Read a valid receipt-retry marker, ignoring interrupted writes."""
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write_activation_state(path: Path, state: dict) -> None:
    """Atomically persist the successful shim before attempting the receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _clear_activation_state(path: Path) -> None:
    """Remove a delivered receipt marker."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class _DeployLock:
    """A non-blocking ``flock`` so overlapping agent processes serialize deploys."""

    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser()
        self._file = None

    def acquire(self) -> bool:
        """Try to take the lock.

        Returns ``True`` when this process holds the lock and ``False`` ONLY
        when another agent currently holds it (flock contention).  Any other
        failure — the lockfile cannot be created or opened, e.g. its directory
        is not writable — raises ``OSError`` so the caller reports the real
        error and backs off instead of misreading it as lock contention.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+")  # OSError (e.g. EACCES) propagates
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False  # another agent holds the lock
            raise
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is not None:
            try:
                fcntl.flock(self._file, fcntl.LOCK_UN)
            except OSError:
                pass
            self._file.close()
            self._file = None


def _run_cycle(agent_args: argparse.Namespace) -> int:
    """Run one poll+execute cycle under the deploy lock (returns exit code)."""
    lock = _DeployLock(agent_args.lockfile)
    try:
        if not lock.acquire():
            logger.info("another deploy agent holds the lock; skipping this cycle")
            return 0
    except OSError as exc:
        # A lockfile that cannot be created/opened (e.g. unwritable directory)
        # is a real failure, not contention: report it and back off rather than
        # silently skipping every cycle with a misleading message.
        logger.error(
            "cannot create/open lockfile %s: %s (backing off)",
            agent_args.lockfile,
            exc,
        )
        return 1
    try:
        return _execute_pending_activation(agent_args)
    finally:
        lock.release()


def _check_preflight(agent_args: argparse.Namespace) -> int:
    """``--check`` preflight: fail closed when the host cannot deploy safely."""
    from blueprint import load_config  # noqa: PLC0415

    try:
        config = load_config()
    except SystemExit:
        logger.error("preflight failed: no Blueprint API config")
        return 1
    if config.get("read_only"):
        logger.error(
            "preflight failed: the configured credential is read-only and "
            "cannot post an activation receipt"
        )
        return 1
    try:
        project_id = get_project_id(agent_args.project_id)
    except SystemExit:
        logger.error("preflight failed: cannot resolve the target project")
        return 1
    if not agent_args.project_id:
        agent_args.project_id = project_id
    if not os.access(_DEFAULT_SHIM, os.X_OK):
        logger.error(
            "preflight failed: %s is not installed/executable; install the "
            "root-owned shim (scripts/blueprint_release.py) first",
            _DEFAULT_SHIM,
        )
        return 1
    logger.info("preflight passed (project %s, shim present)", project_id)
    return 0


def run_forever(agent_args: argparse.Namespace) -> int:
    """Poll the backend indefinitely, executing approved bindings as they land."""
    interval = agent_args.interval
    logger.info(
        "deploy agent started; project=%s host=%s poll_interval=%ss",
        get_project_id(agent_args.project_id) if agent_args.project_id else "default",
        agent_args.host,
        interval,
    )
    while True:
        started = time.monotonic()
        try:
            result = _run_cycle(agent_args)
        except Exception as exc:  # noqa: BLE001 - never crash the service
            logger.exception("unexpected cycle error; backing off: %s", exc)
            result = 1
        elapsed = time.monotonic() - started
        delay = interval * (_BACKOFF_MULTIPLIER if result != 0 else 1)
        if result != 0:
            logger.warning("cycle failed (backoff %ss); next poll in %ss", delay, delay)
        time.sleep(max(0.0, delay - elapsed))


def _handle_sigterm(_signum, _frame) -> None:  # pragma: no cover - process signal
    raise SystemExit(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blueprint_deploy_agent",
        description=__doc__,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll/execute cycle and exit (tests, one-shot).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=_POLL_INTERVAL_SECONDS,
        help=f"Poll interval in seconds (default {_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Target project id (default: resolved from config / BLUEPRINT_PROJECT_ID).",
    )
    parser.add_argument(
        "--host",
        default=socket.gethostname(),
        help="Host identity reported on the activation receipt (default: hostname).",
    )
    parser.add_argument(
        "--lockfile",
        default=_DEFAULT_LOCKFILE,
        help=f"Serialization lockfile path (default {_DEFAULT_LOCKFILE}).",
    )
    parser.add_argument(
        "--state-path",
        default=_DEFAULT_STATE_PATH,
        help=f"Receipt-retry marker path (default {_DEFAULT_STATE_PATH}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and validate the pending binding but do not activate.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if not os.environ.get("BLUEPRINT_DEPLOY_AGENT_QUIET"):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    args = argv if argv is not None else sys.argv[1:]
    if args == ["--check"]:
        check_args = build_parser().parse_args([])
        return _check_preflight(check_args)
    parsed = build_parser().parse_args(args)
    if parsed.project_id:
        try:
            parsed.project_id = get_project_id(parsed.project_id)
        except SystemExit:
            logger.error("cannot resolve project id %s", parsed.project_id)
            return 1
    signal.signal(signal.SIGTERM, _handle_sigterm)
    if parsed.once:
        return _run_cycle(parsed)
    return run_forever(parsed)


if __name__ == "__main__":
    raise SystemExit(main())
