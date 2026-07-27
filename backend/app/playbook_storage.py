"""Canonical playbook storage and lifecycle helpers.

This module owns persistent playbook storage policy: stale-reference scans,
pending-entry finalization, capacity eviction, archival/suppression, tombstone
management, and auto-retirement counters. ``app.playbook_learning`` re-exports
the legacy names so existing callers and tests remain compatible.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, _utcnow

logger = logging.getLogger(__name__)


# Total playbook size (JSON-serialised) above which the least-useful entry is
# archived before a new one is added.
_PLAYBOOK_CAPACITY_BYTES = 30 * 1024

# Cap on playbook_archive length. The archive exists for operator review of
# retired playbook entries; there is no functional reason to keep more than
# the most-recent N. Without this cap the archive grows monotonically and can
# bloat the design_doc past the agent payload budget. Configurable via
# runtime_config.
_PLAYBOOK_ARCHIVE_MAX_DEFAULT = 50

# Entries older than this (in days) with fewer than _STALE_CONFIRM_THRESHOLD
# confirmed uses are deprioritised during the claim sort.
_STALE_AGE_DAYS = 30
_STALE_CONFIRM_THRESHOLD = 3

# Minimum followed_count before success rate replaces raw confirmed_count in
# archival ranking and retirement decisions.
_MIN_FOLLOWED_FOR_RATE = 3

# Auto-retirement: entries followed >= _RETIRE_MIN_FOLLOWED times with a success
# rate below _RETIRE_MAX_SUCCESS_RATE are archived with a _retirement_reason tag.
_RETIRE_MIN_FOLLOWED = 5
_RETIRE_MAX_SUCCESS_RATE = 0.20

# Time-decay retirement for auto-generated playbook entries.
_IDLE_RETIRE_DAYS = 30

# Cooldown window (days) during which a recently-archived auto-playbook entry
# cannot be re-created.
_AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS = 30

# Review-extracted playbook entries are flagged potentially_stale once they
# exceed this age without being refreshed.
_REVIEW_PLAYBOOK_STALE_DAYS = 60

# Operator-deleted keys are retained outside the archive so suppression survives
# archive rotation.
_PLAYBOOK_DELETED_KEYS_MAX = 200

_FILE_PATH_RE = re.compile(
    r"\b((?:backend|agent|admin-ui)/(?:[\w.-]+/)*[\w.-]+\.\w+)\b"
)

# Regex used by self-healing recovery to extract the original status from a
# grounding warning written before grounding_prior_status was added (legacy).
_CLAIMED_STATUS_RE = re.compile(r"\bclaimed\s+(\w+)\b")

# Marker appended to the text field of a grounded entry; stripped during recovery.
_RECONCILED_MARKER_RE = re.compile(r"\s*\[reconciled:[^\]]*\]")


def _playbook_archive_max() -> int:
    from app.shared_state import runtime_config as _rc

    return _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)


def _extract_file_paths(content: str) -> list[str]:
    """Return unique source-file paths found in *content*."""
    return list(dict.fromkeys(_FILE_PATH_RE.findall(content)))


def check_and_flag_stale_refs(doc: dict, repo_root: str) -> dict:
    """Mark playbook entries whose referenced file paths no longer exist."""
    if not repo_root or not os.path.isdir(repo_root):
        return doc

    updated = False
    for key, value in doc.items():
        if not key.startswith("playbook/") or not isinstance(value, dict):
            continue
        paths = _extract_file_paths(value.get("content", ""))
        if not paths:
            continue

        any_missing = any(
            not os.path.isfile(os.path.join(repo_root, path)) for path in paths
        )
        if any_missing and not value.get("stale_file_refs"):
            entry = dict(value)
            entry["stale_file_refs"] = True
            doc[key] = entry
            updated = True
            logger.debug(
                "[PLAYBOOK] Flagged '%s' as stale_file_refs (missing paths: %s)",
                key,
                [
                    path
                    for path in paths
                    if not os.path.isfile(os.path.join(repo_root, path))
                ],
            )
        elif not any_missing and value.get("stale_file_refs"):
            entry = dict(value)
            entry["stale_file_refs"] = False
            doc[key] = entry
            updated = True

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "stale-ref-check"
        doc["_meta"] = meta

    return doc


# Statuses that assert the referenced code is PRESENT (and therefore
# improvable). Anything else (stub/missing/planned) already tells the agent to
# build rather than improve, so it needs no grounding. Mirrors the agent-side
# sets in agent/src/utils/design-doc-presence.ts — keep parity.
_PRESENT_SYSTEM_STATUSES = frozenset({"mature", "functional", "partial"})
_PRESENT_CAPABILITY_STATUSES = frozenset({"working", "partial"})

_GROUNDING_CAP_OVERRIDE_KEY = "playbook_design_doc_grounding_max_downgrades"

# A module pointer may name a specific symbol (``path/to/file.py:symbol``). For
# file-level existence we want the bare path; the symbol is checked separately
# by :func:`_absent_symbol_in_present_file`. Only a symbol suffix (starts with a
# letter/underscore) is stripped — a ``:42`` line-number suffix is left intact.
_MODULE_SYMBOL_SUFFIX_RE = re.compile(r"^(.+\.\w+):[A-Za-z_]\w*$")


def _get_prior_grounding_status(value: dict) -> str | None:
    """Return the pre-downgrade status for a grounding-downgraded entry.

    Prefers the machine-readable ``grounding_prior_status`` field written by the
    current sweep; falls back to parsing ``claimed <status>`` from the
    ``presence_warning`` text for legacy entries downgraded before that field was
    introduced.
    """
    prior = value.get("grounding_prior_status")
    if isinstance(prior, str) and prior:
        return prior
    warning = value.get("presence_warning", "")
    if isinstance(warning, str):
        m = _CLAIMED_STATUS_RE.search(warning)
        if m:
            return m.group(1)
    return None


def _extract_entry_paths(entry: dict) -> list[str]:
    """Collect unique file-path pointers for a design-doc system/capability.

    Mirrors ``extractEntryPaths`` in agent/src/utils/design-doc-presence.ts:
    the explicit ``modules`` array (systems) plus any source paths embedded in
    the entry's ``notes`` / ``description`` free text (capabilities carry
    pointers only in ``notes``).
    """
    paths: list[str] = []
    modules = entry.get("modules")
    if isinstance(modules, list):
        for module in modules:
            if isinstance(module, str) and module.strip():
                stripped = module.strip()
                symbol_match = _MODULE_SYMBOL_SUFFIX_RE.match(stripped)
                paths.append(symbol_match.group(1) if symbol_match else stripped)
    text_parts = [
        entry[key]
        for key in ("notes", "description")
        if isinstance(entry.get(key), str) and entry[key]
    ]
    if text_parts:
        paths.extend(_FILE_PATH_RE.findall("\n".join(text_parts)))
    return list(dict.fromkeys(paths))


def _grounding_provenance(
    source: str,
    *,
    branch: str | None = None,
    sha: str | None = None,
) -> dict:
    provenance = {"checked_at": _utcnow().isoformat(), "source": source}
    if branch:
        provenance["branch"] = branch
    if sha:
        provenance["sha"] = sha
    return provenance


def _grounding_downgrade_cap(present_count: int) -> int:
    from app.shared_state import runtime_config as _rc

    override = _rc.get(_GROUNDING_CAP_OVERRIDE_KEY)
    if isinstance(override, int) and override > 0:
        return override
    if isinstance(override, str):
        try:
            parsed = int(override)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return max(3, math.ceil(present_count * 0.10))


def _grounding_cap_breach_diagnostic(
    *,
    source: str,
    candidates: list[dict],
    cap: int,
) -> dict:
    return {
        "kind": "cap_breach",
        "checked_at": _utcnow().isoformat(),
        "source": source,
        "candidate_count": len(candidates),
        "cap": cap,
        "affected_keys": [candidate["key"] for candidate in candidates],
        "indeterminate": True,
    }


def _apply_grounding_candidate(
    doc: dict,
    *,
    key: str,
    value: dict,
    status: str,
    warning: str,
    detail: str,
    source: str,
    branch: str | None = None,
    sha: str | None = None,
) -> None:
    entry = dict(value)
    entry["grounding_prior_status"] = status
    entry["status"] = "missing"
    entry["presence_warning"] = warning
    entry["grounding_provenance"] = _grounding_provenance(
        source, branch=branch, sha=sha
    )
    _marker_field = "description" if key.startswith("systems/") else "notes"
    existing_text = entry.get(_marker_field) or ""
    marker = f"[reconciled: {warning}]"
    entry[_marker_field] = (
        f"{existing_text} {marker}".lstrip() if existing_text else marker
    )
    doc[key] = entry
    logger.debug("[DESIGN-DOC] Grounded '%s' to missing (%s)", key, detail)


def _is_glob_path(path: str) -> bool:
    """Return True for literal glob patterns that must not confirm absence."""
    return any(ch in path for ch in "*?[]{}")


def _absent_symbol_in_present_file(entry: dict, repo_root: str) -> str | None:
    """Return a ``path:symbol`` ref whose symbol is unambiguously absent from a
    file that still exists, or ``None``.

    Symbol-level companion to the file-level absence check in
    :func:`ground_design_doc_systems`. A present-status entry can cite a
    specific function/class via a ``path:symbol`` pointer (in its ``modules``
    list or its ``notes`` / ``description`` free text) whose file still exists
    while the symbol that implemented the behavior was deleted or moved — a
    common shape in this codebase, where thin re-export facades and post-refactor
    stubs are routine. The file-existence check then passes even though the cited
    behavior is not actually built.

    Reuses the proven ``path:symbol`` parsing and definition regex from
    :mod:`app.source_ref_validation` (the same machinery that grounds research
    finding claims) but reads the file from *repo_root* synchronously, matching
    this module's directory-based grounding rather than the git-backed path.

    Conservative by construction, mirroring the file-level semantics: a ref is
    returned ONLY on an unambiguous absence — the file exists on disk but no
    plausible definition of the symbol is found. A missing or unreadable file
    (indeterminate) and a present symbol both yield ``None``, so a partial or
    indeterminate result is never flagged.
    """
    from app.source_ref_validation import (
        _parse_path_symbol_refs,
        _symbol_definition_regex,
    )

    texts: list[str] = []
    modules = entry.get("modules")
    if isinstance(modules, list):
        texts.extend(m for m in modules if isinstance(m, str) and m)
    texts.extend(
        entry[key]
        for key in ("notes", "description")
        if isinstance(entry.get(key), str) and entry[key]
    )

    seen: set = set()
    for text in texts:
        for path, symbol in _parse_path_symbol_refs(text):
            if (path, symbol) in seen:
                continue
            seen.add((path, symbol))
            full = os.path.join(repo_root, path)
            if not os.path.isfile(full):
                # File absent → a file-level concern (or indeterminate), not a
                # symbol verdict. Leave it to the file-level check.
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                # Unreadable → indeterminate; degrade gracefully, never flag.
                continue
            if not _symbol_definition_regex(symbol).search(content):
                return f"{path}:{symbol}"
    return None


def _present_symbol_in_present_file(entry: dict, repo_root: str) -> bool | None:
    """Return True when a cited symbol is confirmed present in a local file.

    False means at least one present file was read and the cited symbol was
    unambiguously absent. None means no cited symbol could be confirmed present
    or absent because every read was indeterminate.
    """
    from app.source_ref_validation import (
        _parse_path_symbol_refs,
        _symbol_definition_regex,
    )

    texts: list[str] = []
    modules = entry.get("modules")
    if isinstance(modules, list):
        texts.extend(m for m in modules if isinstance(m, str) and m)
    texts.extend(
        entry[key]
        for key in ("notes", "description")
        if isinstance(entry.get(key), str) and entry[key]
    )

    seen: set = set()
    any_absent = False
    any_indeterminate = False
    for text in texts:
        for path, symbol in _parse_path_symbol_refs(text):
            if (path, symbol) in seen:
                continue
            seen.add((path, symbol))
            if _is_glob_path(path):
                any_indeterminate = True
                continue
            full = os.path.join(repo_root, path)
            if not os.path.isfile(full):
                any_indeterminate = True
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                any_indeterminate = True
                continue
            if _symbol_definition_regex(symbol).search(content):
                return True
            any_absent = True

    if any_absent:
        return False
    if any_indeterminate:
        return None
    return None


def ground_design_doc_systems(doc: dict, repo_root: str) -> dict:
    """Ground design-doc ``systems/*`` and ``capabilities/*`` against the tree.

    A PRESENT-status entry (mature/functional/partial for systems,
    working/partial for capabilities) is downgraded to ``missing`` with a
    persisted ``presence_warning`` — so the correction survives across cycles and
    surfaces to the design-doc maintenance and strategic agents — when EITHER:

      - *file-level absence*: it carries at least one backing-module path pointer
        AND every one of those paths is absent under *repo_root*, OR
      - *symbol-level absence*: it cites a specific ``path:symbol`` pointer whose
        file still exists under *repo_root* but whose symbol is unambiguously gone
        (see :func:`_absent_symbol_in_present_file`). This catches re-export
        facades and post-refactor stubs where the file survives but the symbol
        that implemented the behavior was deleted or moved.

    Conservative by construction (mirrors
    agent/src/utils/design-doc-presence.ts):
      - no-op when *repo_root* is falsy or not a directory,
      - partial hits (some paths present) are left untouched at the file level —
        the code partially exists, so the present-status claim is plausible,
      - entries with no path pointer are left untouched (absence of a path is
        not evidence of absent code),
      - a symbol is flagged only on an unambiguous absence; an indeterminate
        result (file gone/unreadable) or a present symbol never flags.

    Idempotent: once downgraded to ``missing`` the entry falls out of the
    present-status set and is skipped on subsequent sweeps. Mutates and returns
    *doc* (same contract as :func:`check_and_flag_stale_refs`).
    """
    if not repo_root or not os.path.isdir(repo_root):
        return doc

    candidates: list[dict] = []
    present_count = 0
    for key, value in doc.items():
        if not isinstance(value, dict):
            continue
        if key.startswith("systems/"):
            present_set = _PRESENT_SYSTEM_STATUSES
        elif key.startswith("capabilities/"):
            present_set = _PRESENT_CAPABILITY_STATUSES
        else:
            continue

        status_raw = value.get("status")
        status = status_raw.lower() if isinstance(status_raw, str) else ""
        if status not in present_set:
            continue
        present_count += 1

        paths = _extract_entry_paths(value)
        concrete_paths = [path for path in paths if not _is_glob_path(path)]

        # File-level absence: downgrade only when EVERY referenced path is gone.
        # No pointer at all → absence of a path is NOT evidence of absent code; a
        # partial hit means the code partially exists, so the claim is plausible.
        file_absent = bool(concrete_paths) and all(
            not os.path.exists(os.path.join(repo_root, path))
            for path in concrete_paths
        )

        # Symbol-level absence: a cited symbol is gone even though its file still
        # exists. Complementary to (not a replacement for) the file-level check —
        # only consulted when the file-level check did not already fire.
        absent_symbol = (
            None if file_absent else _absent_symbol_in_present_file(value, repo_root)
        )

        if not file_absent and absent_symbol is None:
            continue

        if file_absent:
            warning = (
                f"claimed {status} but all referenced module file(s) are absent "
                f"on the integration branch — treat as missing"
            )
            detail = f"all referenced paths absent: {concrete_paths}"
        else:
            warning = (
                f"claimed {status} but cited symbol '{absent_symbol}' is absent on "
                f"the integration branch though its file exists — treat as missing"
            )
            detail = f"cited symbol absent: {absent_symbol}"
        candidates.append(
            {
                "key": key,
                "value": value,
                "status": status,
                "warning": warning,
                "detail": detail,
            }
        )

    cap = _grounding_downgrade_cap(present_count)
    if len(candidates) > cap:
        logger.warning(
            "[DESIGN-DOC] Grounding cap breach on local-disk source: "
            "candidate_count=%d cap=%d affected_keys=%s",
            len(candidates),
            cap,
            [candidate["key"] for candidate in candidates],
        )
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["grounding_diagnostic"] = _grounding_cap_breach_diagnostic(
            source="local-disk",
            candidates=candidates,
            cap=cap,
        )
        doc["_meta"] = meta
        return doc

    updated = False
    for candidate in candidates:
        _apply_grounding_candidate(
            doc,
            key=candidate["key"],
            value=candidate["value"],
            status=candidate["status"],
            warning=candidate["warning"],
            detail=candidate["detail"],
            source="local-disk",
        )
        updated = True

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta.pop("grounding_diagnostic", None)
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "design-doc-grounding"
        doc["_meta"] = meta

    return doc


# Cooldown (hours) that bounds how often the GitHub-backed claim-path grounding
# may run. The task-claim handler runs on EVERY claim and is best-effort, so the
# GitHub network I/O is throttled to at most once per window per project rather
# than firing on every claim. Configurable via runtime_config.
_BRANCH_GROUNDING_COOLDOWN_HOURS_DEFAULT = 6


def branch_grounding_due(doc: dict, *, cooldown_hours: int | None = None) -> bool:
    """Return True when the GitHub-backed claim-path grounding may run again.

    Keeps the GitHub network I/O off the per-claim hot path: returns False while
    the previous grounding's ``_meta.last_branch_grounding_at`` timestamp is
    within *cooldown_hours*, so the (best-effort) reconcile fires at most once
    per window per project. A missing or unparseable timestamp means due, and a
    non-positive cooldown disables throttling.
    """
    if cooldown_hours is None:
        from app.shared_state import runtime_config as _rc

        cooldown_hours = _rc.get(
            "playbook_branch_grounding_cooldown_hours",
            _BRANCH_GROUNDING_COOLDOWN_HOURS_DEFAULT,
        )
    if cooldown_hours <= 0:
        return True
    ts_str = (doc.get("_meta") or {}).get("last_branch_grounding_at")
    if not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
    except (ValueError, TypeError):
        return True
    return (_utcnow() - ts) >= timedelta(hours=cooldown_hours)


def mark_branch_grounding_done(doc: dict) -> None:
    """Stamp ``_meta.last_branch_grounding_at`` so :func:`branch_grounding_due`
    can bound how often the claim-path grounding runs.

    Bookkeeping only — it does NOT bump the doc revision, so a no-op grounding
    pass leaves the design-doc content (and its ``revision``) untouched.
    """
    meta = dict(doc.get("_meta", {"revision": 0}))
    meta["last_branch_grounding_at"] = _utcnow().isoformat()
    doc["_meta"] = meta


async def _absent_symbol_in_repo_branch(
    entry: dict,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> str | None:
    """Branch-aware sibling of :func:`_absent_symbol_in_present_file`.

    Returns a ``path:symbol`` ref whose symbol is unambiguously absent from a
    file that still exists on ``owner/repo@branch``, or ``None``. Reuses the
    ``path:symbol`` parsing from :mod:`app.source_ref_validation` and checks each
    pointer with :func:`_symbol_defined_in_repo_branch` (GitHub-backed) rather
    than the local checkout. Conservative: a ref is returned ONLY on an
    unambiguous absence (``False``); an indeterminate result (``None``, e.g. the
    file is itself absent or unreadable) and a present symbol both yield
    ``None``, so a missing file is never double-flagged here.
    """
    from app.source_ref_validation import (
        _parse_path_symbol_refs,
        _symbol_defined_in_repo_branch,
    )

    texts: list[str] = []
    modules = entry.get("modules")
    if isinstance(modules, list):
        texts.extend(m for m in modules if isinstance(m, str) and m)
    texts.extend(
        entry[key]
        for key in ("notes", "description")
        if isinstance(entry.get(key), str) and entry[key]
    )

    seen: set = set()
    for text in texts:
        for path, symbol in _parse_path_symbol_refs(text):
            if (path, symbol) in seen:
                continue
            seen.add((path, symbol))
            defined = await _symbol_defined_in_repo_branch(
                path,
                symbol,
                token=token,
                owner=owner,
                repo=repo,
                branch=branch,
                project_id=project_id,
            )
            if defined is False:
                return f"{path}:{symbol}"
    return None


async def _present_symbol_in_repo_branch(
    entry: dict,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> bool | None:
    """Return True when a cited symbol is confirmed present on the branch.

    False means at least one present file was checked and the cited symbol was
    unambiguously absent. None means no cited symbol could be confirmed present
    or absent because every read was indeterminate.
    """
    from app.source_ref_validation import (
        _parse_path_symbol_refs,
        _symbol_defined_in_repo_branch,
    )

    texts: list[str] = []
    modules = entry.get("modules")
    if isinstance(modules, list):
        texts.extend(m for m in modules if isinstance(m, str) and m)
    texts.extend(
        entry[key]
        for key in ("notes", "description")
        if isinstance(entry.get(key), str) and entry[key]
    )

    seen: set = set()
    any_absent = False
    any_indeterminate = False
    for text in texts:
        for path, symbol in _parse_path_symbol_refs(text):
            if (path, symbol) in seen:
                continue
            seen.add((path, symbol))
            defined = await _symbol_defined_in_repo_branch(
                path,
                symbol,
                token=token,
                owner=owner,
                repo=repo,
                branch=branch,
                project_id=project_id,
            )
            if defined is True:
                return True
            if defined is False:
                any_absent = True
            else:
                any_indeterminate = True

    if any_absent:
        return False
    if any_indeterminate:
        return None
    return None


async def check_and_flag_stale_refs_branch_aware(
    doc: dict,
    *,
    repo_root: str | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Branch-aware sibling of :func:`check_and_flag_stale_refs`.

    Flags playbook entries whose referenced file paths are absent on the
    project's REAL integration branch (via the GitHub contents API, using
    :func:`app.source_ref_validation.classify_existing_paths`) instead of a local
    checkout that does not exist on the backend. When GitHub credentials do not
    resolve it falls back to the synchronous local-disk scan against *repo_root*
    (a no-op when no checkout is configured), so a backend with neither creds nor
    a checkout never invents stale flags.

    Same mutate-and-return contract and ``stale_file_refs`` semantics as the sync
    function: an entry is flagged when at least one referenced path is missing
    and the flag is cleared when all referenced paths are present again.
    """
    use_repo_branch = all([token, owner, repo, branch])
    if not use_repo_branch:
        return check_and_flag_stale_refs(doc, repo_root or "")

    from app.source_ref_validation import classify_paths_tristate

    updated = False
    for key, value in doc.items():
        if not key.startswith("playbook/") or not isinstance(value, dict):
            continue
        paths = _extract_file_paths(value.get("content", ""))
        if not paths:
            continue
        _present, missing, indeterminate = await classify_paths_tristate(
            paths,
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )
        # Indeterminate paths (rate limit / quota reserve / API error) are not
        # evidence either way: never flag on them, and never clear an existing
        # flag while any path is unknowable — leave the entry untouched.
        if indeterminate and not missing:
            continue
        any_missing = bool(missing)
        if any_missing and not value.get("stale_file_refs"):
            entry = dict(value)
            entry["stale_file_refs"] = True
            doc[key] = entry
            updated = True
            logger.debug(
                "[PLAYBOOK] Flagged '%s' as stale_file_refs (missing on branch: %s)",
                key,
                missing,
            )
        elif not any_missing and value.get("stale_file_refs"):
            entry = dict(value)
            entry["stale_file_refs"] = False
            doc[key] = entry
            updated = True

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "stale-ref-check"
        doc["_meta"] = meta

    return doc


async def ground_design_doc_systems_branch_aware(
    doc: dict,
    *,
    repo_root: str | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    sha: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Branch-aware sibling of :func:`ground_design_doc_systems`.

    Grounds present-status ``systems/*`` and ``capabilities/*`` entries against
    the project's REAL integration branch via the GitHub contents API (the same
    mechanism the strategic/research design-doc reconcile uses) instead of a
    local checkout that does not exist on the backend. When GitHub credentials
    do not resolve it falls back to the synchronous local-disk grounding against
    *repo_root* (a no-op when no checkout is configured).

    File-level and symbol-level absence are handled exactly as in the sync
    function — only the existence/definition source differs (GitHub branch vs.
    local disk):

      - *file-level absence*: every referenced backing path is absent on the
        branch (``_file_exists_in_repo_branch``), OR
      - *symbol-level absence*: a cited ``path:symbol`` whose file still exists on
        the branch but whose symbol is unambiguously gone
        (:func:`_absent_symbol_in_repo_branch`).

    Conservative tri-state semantics are preserved: entries with no path pointer
    and partial file hits are left untouched, and a symbol downgrades only on an
    unambiguous absence. Mutates and returns *doc* (same contract as the sync
    function).
    """
    use_repo_branch = all([token, owner, repo, branch])
    if not use_repo_branch:
        if not repo_root or not os.path.isdir(repo_root):
            return doc
        candidates: list[dict] = []
        present_count = 0
        for key, value in doc.items():
            if not isinstance(value, dict):
                continue
            if key.startswith("systems/"):
                present_set = _PRESENT_SYSTEM_STATUSES
            elif key.startswith("capabilities/"):
                present_set = _PRESENT_CAPABILITY_STATUSES
            else:
                continue

            status_raw = value.get("status")
            status = status_raw.lower() if isinstance(status_raw, str) else ""
            if status not in present_set:
                continue
            present_count += 1

            paths = _extract_entry_paths(value)
            concrete_paths = [path for path in paths if not _is_glob_path(path)]
            file_absent = bool(concrete_paths) and all(
                not os.path.exists(os.path.join(repo_root, path))
                for path in concrete_paths
            )
            absent_symbol = (
                None if file_absent else _absent_symbol_in_present_file(value, repo_root)
            )
            if not file_absent and absent_symbol is None:
                continue

            if file_absent:
                warning = (
                    f"claimed {status} but all referenced module file(s) are absent "
                    f"on the integration branch — treat as missing"
                )
                detail = f"all referenced paths absent: {concrete_paths}"
            else:
                warning = (
                    f"claimed {status} but cited symbol '{absent_symbol}' is absent on "
                    f"the integration branch though its file exists — treat as missing"
                )
                detail = f"cited symbol absent: {absent_symbol}"
            candidates.append(
                {
                    "key": key,
                    "value": value,
                    "status": status,
                    "warning": warning,
                    "detail": detail,
                }
            )

        cap = _grounding_downgrade_cap(present_count)
        if len(candidates) > cap:
            logger.warning(
                "[DESIGN-DOC] Grounding cap breach on local-disk fallback: "
                "candidate_count=%d cap=%d affected_keys=%s",
                len(candidates),
                cap,
                [candidate["key"] for candidate in candidates],
            )
            meta = dict(doc.get("_meta", {"revision": 0}))
            meta["grounding_diagnostic"] = _grounding_cap_breach_diagnostic(
                source="local-disk",
                candidates=candidates,
                cap=cap,
            )
            doc["_meta"] = meta
            return doc

        updated = False
        for candidate in candidates:
            _apply_grounding_candidate(
                doc,
                key=candidate["key"],
                value=candidate["value"],
                status=candidate["status"],
                warning=candidate["warning"],
                detail=candidate["detail"],
                source="local-disk",
                branch=branch,
                sha=sha,
            )
            updated = True

        if updated:
            meta = dict(doc.get("_meta", {"revision": 0}))
            meta.pop("grounding_diagnostic", None)
            meta["revision"] = meta.get("revision", 0) + 1
            meta["last_updated"] = _utcnow().isoformat()
            meta["updated_by"] = "design-doc-grounding"
            doc["_meta"] = meta

        return doc

    from app.source_ref_validation import _file_exists_in_repo_branch

    candidates: list[dict] = []
    present_count = 0
    for key, value in doc.items():
        if not isinstance(value, dict):
            continue
        if key.startswith("systems/"):
            present_set = _PRESENT_SYSTEM_STATUSES
        elif key.startswith("capabilities/"):
            present_set = _PRESENT_CAPABILITY_STATUSES
        else:
            continue

        status_raw = value.get("status")
        status = status_raw.lower() if isinstance(status_raw, str) else ""
        if status not in present_set:
            continue
        present_count += 1

        paths = _extract_entry_paths(value)

        # File-level absence: downgrade only when EVERY referenced path is
        # UNAMBIGUOUSLY gone on the branch. No pointer, a partial hit, or any
        # indeterminate lookup (rate limit / quota reserve / API error) leaves
        # the entry untouched — an outage is never evidence of absent code.
        file_absent = False
        if paths:
            any_present = False
            any_indeterminate = False
            for path in paths:
                exists = await _file_exists_in_repo_branch(
                    path,
                    token=str(token),
                    owner=str(owner),
                    repo=str(repo),
                    branch=str(branch),
                    project_id=project_id,
                )
                if exists:
                    any_present = True
                    break
                if exists is None:
                    any_indeterminate = True
            file_absent = not any_present and not any_indeterminate

        # Symbol-level absence: only consulted when the file-level check did not
        # already fire (complementary, not a replacement).
        absent_symbol = (
            None
            if file_absent
            else await _absent_symbol_in_repo_branch(
                value,
                token=str(token),
                owner=str(owner),
                repo=str(repo),
                branch=str(branch),
                project_id=project_id,
            )
        )

        if not file_absent and absent_symbol is None:
            continue

        if file_absent:
            warning = (
                f"claimed {status} but all referenced module file(s) are absent "
                f"on the integration branch — treat as missing"
            )
            detail = f"all referenced paths absent: {paths}"
        else:
            warning = (
                f"claimed {status} but cited symbol '{absent_symbol}' is absent on "
                f"the integration branch though its file exists — treat as missing"
            )
            detail = f"cited symbol absent: {absent_symbol}"
        candidates.append(
            {
                "key": key,
                "value": value,
                "status": status,
                "warning": warning,
                "detail": detail,
            }
        )

    cap = _grounding_downgrade_cap(present_count)
    source = "github-snapshot"
    if len(candidates) > cap:
        logger.warning(
            "[DESIGN-DOC] Grounding cap breach on %s source: "
            "candidate_count=%d cap=%d affected_keys=%s",
            source,
            len(candidates),
            cap,
            [candidate["key"] for candidate in candidates],
        )
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["grounding_diagnostic"] = _grounding_cap_breach_diagnostic(
            source=source,
            candidates=candidates,
            cap=cap,
        )
        doc["_meta"] = meta
        return doc

    updated = False
    for candidate in candidates:
        _apply_grounding_candidate(
            doc,
            key=candidate["key"],
            value=candidate["value"],
            status=candidate["status"],
            warning=candidate["warning"],
            detail=candidate["detail"],
            source=source,
            branch=branch,
            sha=sha,
        )
        updated = True

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta.pop("grounding_diagnostic", None)
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "design-doc-grounding"
        doc["_meta"] = meta

    return doc


def recover_grounding_downgrades(doc: dict, repo_root: str) -> dict:
    """Inverse pass of :func:`ground_design_doc_systems`: restore falsely-downgraded entries.

    Examines ``status=missing`` entries that carry a ``presence_warning`` (set by
    the grounding sweep). For each such entry, if at least one referenced backing
    path is CONFIRMED present on disk the entry is restored: its prior status is
    reinstated, ``presence_warning`` and ``grounding_prior_status`` are cleared,
    and any ``[reconciled: ...]`` marker is stripped from text fields.

    Conservative tri-state semantics throughout: only a confirmed presence
    (``os.path.exists`` returning True) triggers recovery. Indeterminate states
    (no checkout, unreadable file) leave the entry untouched.

    For symbol-level downgrades (warning contains "cited symbol") the cited symbol
    must additionally be confirmed defined in its file before restoring.

    Authored-missing entries (no ``presence_warning``) are never touched.
    """
    if not repo_root or not os.path.isdir(repo_root):
        return doc

    updated = False
    for key, value in doc.items():
        if not isinstance(value, dict):
            continue
        if not (key.startswith("systems/") or key.startswith("capabilities/")):
            continue
        if value.get("status") != "missing":
            continue
        presence_warning = value.get("presence_warning")
        if not presence_warning:
            continue
        prior_status = _get_prior_grounding_status(value)
        if not prior_status:
            continue

        paths = _extract_entry_paths(value)
        if not paths:
            continue

        is_symbol_downgrade = "cited symbol" in str(presence_warning)
        if is_symbol_downgrade:
            confirmed_recovery = (
                _present_symbol_in_present_file(value, repo_root) is True
            )
        else:
            confirmed_recovery = any(
                os.path.exists(os.path.join(repo_root, p)) for p in paths
            )

        if not confirmed_recovery:
            continue

        entry = dict(value)
        entry["status"] = prior_status
        entry.pop("presence_warning", None)
        entry.pop("grounding_prior_status", None)
        entry.pop("grounding_provenance", None)
        for text_field in ("description", "notes"):
            if isinstance(entry.get(text_field), str):
                cleaned = _RECONCILED_MARKER_RE.sub("", entry[text_field]).rstrip()
                if cleaned:
                    entry[text_field] = cleaned
                else:
                    del entry[text_field]
        doc[key] = entry
        updated = True
        logger.debug(
            "[DESIGN-DOC] Recovered '%s' to %s (confirmed present on disk)",
            key,
            prior_status,
        )

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "design-doc-grounding-recovery"
        doc["_meta"] = meta

    return doc


async def recover_grounding_downgrades_branch_aware(
    doc: dict,
    *,
    repo_root: str | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Branch-aware sibling of :func:`recover_grounding_downgrades`.

    Restores falsely-downgraded ``status=missing`` entries by checking whether
    backing paths are CONFIRMED present on the integration branch via the GitHub
    contents API. Falls back to the synchronous local-disk recovery when GitHub
    credentials do not resolve.

    Conservative: only a confirmed presence (``True`` from
    ``_file_exists_in_repo_branch``) triggers recovery; indeterminate results
    (``None``, e.g. rate limit / API outage) leave the entry untouched.
    """
    use_repo_branch = all([token, owner, repo, branch])
    if not use_repo_branch:
        return recover_grounding_downgrades(doc, repo_root or "")

    from app.source_ref_validation import _file_exists_in_repo_branch

    updated = False
    for key, value in doc.items():
        if not isinstance(value, dict):
            continue
        if not (key.startswith("systems/") or key.startswith("capabilities/")):
            continue
        if value.get("status") != "missing":
            continue
        presence_warning = value.get("presence_warning")
        if not presence_warning:
            continue
        prior_status = _get_prior_grounding_status(value)
        if not prior_status:
            continue

        paths = _extract_entry_paths(value)
        if not paths:
            continue

        is_symbol_downgrade = "cited symbol" in str(presence_warning)
        if is_symbol_downgrade:
            confirmed_recovery = (
                await _present_symbol_in_repo_branch(
                    value,
                    token=str(token),
                    owner=str(owner),
                    repo=str(repo),
                    branch=str(branch),
                    project_id=project_id,
                )
                is True
            )
        else:
            confirmed_recovery = False
            for path in paths:
                exists = await _file_exists_in_repo_branch(
                    path,
                    token=str(token),
                    owner=str(owner),
                    repo=str(repo),
                    branch=str(branch),
                    project_id=project_id,
                )
                if exists is True:
                    confirmed_recovery = True
                    break

        if not confirmed_recovery:
            continue

        entry = dict(value)
        entry["status"] = prior_status
        entry.pop("presence_warning", None)
        entry.pop("grounding_prior_status", None)
        entry.pop("grounding_provenance", None)
        for text_field in ("description", "notes"):
            if isinstance(entry.get(text_field), str):
                cleaned = _RECONCILED_MARKER_RE.sub("", entry[text_field]).rstrip()
                if cleaned:
                    entry[text_field] = cleaned
                else:
                    del entry[text_field]
        doc[key] = entry
        updated = True
        logger.debug(
            "[DESIGN-DOC] Recovered '%s' to %s on branch (confirmed present)",
            key,
            prior_status,
        )

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "design-doc-grounding-recovery"
        doc["_meta"] = meta

    return doc


def _playbook_size_bytes(doc: dict) -> int:
    """Return the JSON-serialised byte size of all playbook entries in *doc*."""
    playbook_subset = {
        key: value for key, value in doc.items() if key.startswith("playbook/")
    }
    return len(json.dumps(playbook_subset).encode())


def _append_archived_playbook_entry(
    doc: dict,
    full_key: str,
    archived_entry: dict,
    *,
    archived_at: str | None = None,
) -> int:
    """Append *archived_entry* to playbook_archive and trim to the configured cap."""
    archive = list(doc.get("playbook_archive") or [])
    archived_payload = dict(archived_entry)
    archived_payload["_archived_key"] = full_key
    archived_payload["_archived_at"] = archived_at or _utcnow().isoformat()
    archive.append(archived_payload)

    archive_max = _playbook_archive_max()
    dropped = 0
    if archive_max > 0 and len(archive) > archive_max:
        dropped = len(archive) - archive_max
        archive = archive[-archive_max:]
    doc["playbook_archive"] = archive
    return dropped


def archive_playbook_entry(
    doc: dict,
    full_key: str,
    *,
    archived_at: str | None = None,
    retirement_reason: str | None = None,
) -> bool:
    """Move an active playbook entry into playbook_archive.

    Returns ``True`` when the key existed and was archived, ``False`` otherwise.
    """
    entry = doc.get(full_key)
    if not isinstance(entry, dict):
        return False

    victim = dict(doc.pop(full_key))
    if retirement_reason is not None:
        victim["_retirement_reason"] = retirement_reason
    _append_archived_playbook_entry(doc, full_key, victim, archived_at=archived_at)
    return True


def record_playbook_tombstone(
    doc: dict,
    full_key: str,
    *,
    archived_at: str | None = None,
) -> None:
    """Record an operator-deletion tombstone and durable suppression key (idempotent)."""
    # Skip if an operator_deleted archive row already exists for this key.
    existing_archive = doc.get("playbook_archive") or []
    already_tombstoned = any(
        e.get("_archived_key") == full_key
        and e.get("_retirement_reason") == "operator_deleted"
        for e in existing_archive
    )
    if not already_tombstoned:
        tombstone = {"_retirement_reason": "operator_deleted"}
        _append_archived_playbook_entry(doc, full_key, tombstone, archived_at=archived_at)

    deleted_keys = list(doc.get("playbook_deleted_keys") or [])
    if full_key not in deleted_keys:
        deleted_keys.append(full_key)
    if len(deleted_keys) > _PLAYBOOK_DELETED_KEYS_MAX:
        deleted_keys = deleted_keys[-_PLAYBOOK_DELETED_KEYS_MAX:]
    doc["playbook_deleted_keys"] = deleted_keys


async def finalize_pending_playbook_entries(
    task: Task,
    session: AsyncSession,
    *,
    succeeded: bool,
) -> int:
    """Resolve playbook entries left ``pending_source=<task.id>`` by *task*."""
    project_row = await session.execute(
        select(Project)
        .where(Project.id == task.project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = project_row.scalar_one_or_none()
    if project is None or not project.design_doc:
        return 0

    doc = dict(project.design_doc)
    task_id = str(task.id)
    changed = 0
    to_delete: list[str] = []

    for key, value in list(doc.items()):
        if not key.startswith("playbook/") or not isinstance(value, dict):
            continue
        if value.get("pending_source") != task_id:
            continue
        if succeeded:
            entry = dict(value)
            entry.pop("pending_source", None)
            entry.pop("_pending_previous", None)
            doc[key] = entry
        else:
            previous = value.get("_pending_previous")
            if isinstance(previous, dict) and previous:
                doc[key] = previous
            else:
                to_delete.append(key)
        changed += 1

    for key in to_delete:
        doc.pop(key, None)

    if changed:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = (
            "playbook-finalize-success" if succeeded else "playbook-finalize-failure"
        )
        doc["_meta"] = meta
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        logger.info(
            "[PLAYBOOK] %s %d pending entr%s for terminal task %s (succeeded=%s)",
            "Finalised" if succeeded else "Removed",
            changed,
            "y" if changed == 1 else "ies",
            task.id,
            succeeded,
        )
    return changed


def visible_playbook_entry(
    value: object,
    *,
    viewer_task_id: str | None = None,
) -> dict | None:
    """Return the entry a viewer may trust while an update is pending.

    The source task sees its staged candidate. Other viewers keep seeing the
    prior committed value, when one exists; a brand-new pending claim remains
    hidden until its source task completes.
    """
    if not isinstance(value, dict):
        return None
    pending_source = value.get("pending_source")
    if pending_source and pending_source != viewer_task_id:
        previous = value.get("_pending_previous")
        return previous if isinstance(previous, dict) and previous else None
    visible = dict(value)
    visible.pop("_pending_previous", None)
    return visible


def mutate_visible_playbook_entry(
    value: object,
    *,
    viewer_task_id: str | None,
    mutate: Callable[[dict], None],
) -> dict | None:
    """Apply observation counters to the same entry content the viewer saw."""
    if not isinstance(value, dict):
        return None
    raw = dict(value)
    pending_source = raw.get("pending_source")
    if pending_source and pending_source != viewer_task_id:
        previous = raw.get("_pending_previous")
        if not isinstance(previous, dict) or not previous:
            return None
        previous = dict(previous)
        mutate(previous)
        raw["_pending_previous"] = previous
        return raw
    mutate(raw)
    return raw


def _archive_least_useful_entry(doc: dict) -> dict:
    """Move the least-useful playbook entry to ``playbook_archive`` in *doc*.

    This is the single source of truth for capacity-eviction selection; both
    the storage path (``success_patterns``, ``admin_notifications``, and the
    ``playbook_learning`` re-export) and the capacity path
    (``playbook_capacity.maybe_archive_for_capacity`` — used by
    ``playbook_auto``, ``review_learning`` and ``admin_agent_ops``) route
    through here so the eviction order can never diverge again.

    "Least useful" is determined by a reverse-sort over the tuple
    ``(stale, effectiveness, age_days, key)`` — the entry with the highest
    tuple is archived first:

    1. ``stale_file_refs`` flag (stale entries first).
    2. ``effectiveness``: a *proven low-rate* entry must always rank worse
       (be archived before) an *untried* zero-count entry. To guarantee that,
       entries with enough observations (``followed_count >=
       _MIN_FOLLOWED_FOR_RATE``) use ``1.0 - success_rate`` which lives in
       ``[0, 1]`` (1 == always-fails == worst), while untried/under-observed
       entries fall back to ``-confirmed_count`` which lives in ``[-inf, 0]``
       (0 == never confirmed). Because every rate-based score is ``>= 0`` and
       every raw-count score is ``<= 0``, a measured poor performer is always
       evicted before an unobserved entry.
    3. Entry age (oldest ``updated_at`` first).
    4. Key name (deterministic tie-break).

    Returns the mutated *doc*; unchanged if there are no archiveable entries.
    """
    now = _utcnow()
    candidates: list[tuple[int, float, int, str]] = []
    for key, value in doc.items():
        if not key.startswith("playbook/") or not isinstance(value, dict):
            continue
        confirmed = value.get("confirmed_count", 0)
        followed = value.get("followed_count", 0)
        stale = 1 if value.get("stale_file_refs") else 0
        updated_at_str = value.get("updated_at", "")
        age_days = 0
        if updated_at_str:
            try:
                updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                if updated.tzinfo is not None:
                    updated = updated.replace(tzinfo=None)
                age_days = (now - updated).days
            except (ValueError, TypeError):
                pass
        # Rate-based entries map to [0, 1] (1 == worst, archived first); raw-count
        # entries map to [-inf, 0] (0 == untried). Reverse-sorted, this guarantees a
        # proven low-rate entry archives BEFORE an untried zero-count entry.
        if followed >= _MIN_FOLLOWED_FOR_RATE:
            effectiveness = 1.0 - (confirmed / followed)
        else:
            effectiveness = -confirmed
        candidates.append((stale, effectiveness, age_days, key))

    if not candidates:
        return doc

    candidates.sort(reverse=True)
    _, _, _, victim_key = candidates[0]
    victim = dict(doc.pop(victim_key))
    dropped = _append_archived_playbook_entry(
        doc,
        victim_key,
        victim,
        archived_at=now.isoformat(),
    )
    if dropped:
        logger.info(
            "[PLAYBOOK] Dropped %d oldest archive entries to stay within cap of %d",
            dropped,
            _playbook_archive_max(),
        )
    logger.info(
        "[PLAYBOOK] Archived entry '%s' to playbook_archive (capacity cleanup)",
        victim_key,
    )
    return doc


def _mark_stale_review_entries(doc: dict) -> bool:
    """Flag review-extracted entries whose ``updated_at`` exceeds the stale threshold."""
    now = _utcnow()
    changed = False
    for key, value in doc.items():
        if not key.startswith("playbook/review-") or not isinstance(value, dict):
            continue
        if value.get("source") != "review-extracted":
            continue
        updated_at_str = value.get("updated_at") or ""
        try:
            updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            if updated.tzinfo is not None:
                updated = updated.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if (now - updated).days < _REVIEW_PLAYBOOK_STALE_DAYS or value.get(
            "potentially_stale"
        ):
            continue
        entry = dict(value)
        entry["potentially_stale"] = True
        doc[key] = entry
        changed = True
    return changed


def maybe_archive_for_capacity(doc: dict, new_entry_estimate: int = 0) -> dict:
    """Archive least-useful playbook entries until the incoming write fits."""
    while _playbook_size_bytes(doc) + new_entry_estimate > _PLAYBOOK_CAPACITY_BYTES:
        size_before = _playbook_size_bytes(doc)
        doc = _archive_least_useful_entry(doc)
        if _playbook_size_bytes(doc) >= size_before:
            break
    return doc


def _get_prior_archive_entry(doc: dict, full_key: str) -> dict | None:
    """Return the most recent ``playbook_archive`` entry for *full_key*, or None."""
    for entry in reversed(list(doc.get("playbook_archive") or [])):
        if entry.get("_archived_key") == full_key:
            return entry
    return None


def _is_auto_playbook_suppressed(doc: dict, full_key: str) -> bool:
    """Return True when *full_key* must not be re-created."""
    import app.shared_state as _ss

    if full_key in (doc.get("playbook_deleted_keys") or []):
        return True

    cooldown_days: int = _ss.runtime_config.get(
        "auto_playbook_resurrection_cooldown_days",
        _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
    )
    if cooldown_days <= 0:
        return False

    cutoff = _utcnow() - timedelta(days=cooldown_days)
    for entry in reversed(list(doc.get("playbook_archive") or [])):
        if entry.get("_archived_key") != full_key:
            continue
        archived_at_str = entry.get("_archived_at", "")
        if not archived_at_str:
            return True
        try:
            archived_at = datetime.fromisoformat(archived_at_str.replace("Z", "+00:00"))
            if archived_at.tzinfo is not None:
                archived_at = archived_at.replace(tzinfo=None)
        except (ValueError, TypeError):
            return True
        if archived_at >= cutoff:
            return True
    return False


def record_playbook_applied(doc: dict, key: str) -> None:
    """Increment followed_count when a playbook entry is applied to a task prompt."""
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        updated = dict(entry)
        updated["followed_count"] = updated.get("followed_count", 0) + 1
        doc[full_key] = updated


def record_playbook_confirmed(doc: dict, key: str) -> None:
    """Increment confirmed_count when a task that used this entry succeeds."""
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        updated = dict(entry)
        updated["confirmed_count"] = updated.get("confirmed_count", 0) + 1
        doc[full_key] = updated


def retire_ineffective_patterns(
    doc: dict,
    *,
    min_followed: int = _RETIRE_MIN_FOLLOWED,
    max_success_rate: float = _RETIRE_MAX_SUCCESS_RATE,
) -> list[str]:
    """Auto-archive low-success auto-generated playbook entries."""
    now_iso = _utcnow().isoformat()
    retired: list[str] = []

    for key in list(doc.keys()):
        entry = doc.get(key)
        if not key.startswith("playbook/") or not isinstance(entry, dict):
            continue
        if entry.get("source") != "auto-generated":
            continue
        followed = entry.get("followed_count", 0)
        if followed < min_followed:
            continue
        confirmed = entry.get("confirmed_count", 0)
        success_rate = confirmed / followed
        if success_rate >= max_success_rate:
            continue

        archive_playbook_entry(
            doc,
            key,
            archived_at=now_iso,
            retirement_reason=f"low_success_rate:{success_rate:.2f}",
        )
        retired.append(key)
        logger.info(
            "[PLAYBOOK] Retired '%s' success_rate=%.2f followed=%d confirmed=%d",
            key,
            success_rate,
            followed,
            confirmed,
        )

    return retired


def retire_idle_auto_entries(
    doc: dict,
    *,
    idle_days: int = _IDLE_RETIRE_DAYS,
) -> list[str]:
    """Auto-archive idle auto-generated playbook entries."""
    now = _utcnow()
    now_iso = now.isoformat()
    threshold = timedelta(days=idle_days)
    retired: list[str] = []

    for key in list(doc.keys()):
        entry = doc.get(key)
        if not key.startswith("playbook/") or not isinstance(entry, dict):
            continue
        if entry.get("source") != "auto-generated":
            continue

        ts_str = entry.get("last_referenced_at") or entry.get("updated_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if (now - ts) < threshold:
            continue

        archive_playbook_entry(
            doc,
            key,
            archived_at=now_iso,
            retirement_reason=f"idle_auto_entry:{idle_days}d",
        )
        retired.append(key)
        logger.info(
            "[PLAYBOOK] Retired idle auto-entry '%s' (idle >= %dd)",
            key,
            idle_days,
        )

    return retired


def count_idle_auto_retired_within(doc: dict, *, hours: int = 24) -> int:
    """Count playbook_archive entries retired by ``retire_idle_auto_entries``."""
    if not doc:
        return 0
    archive = doc.get("playbook_archive") or []
    if not archive:
        return 0
    cutoff = _utcnow() - timedelta(hours=hours)
    count = 0
    for entry in archive:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("_retirement_reason") or "").startswith("idle_auto_entry"):
            continue
        ts_str = entry.get("_archived_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            count += 1
    return count


def count_low_success_retired_within(doc: dict, *, hours: int = 24) -> int:
    """Count playbook_archive entries retired by ``retire_ineffective_patterns``."""
    if not doc:
        return 0
    archive = doc.get("playbook_archive") or []
    if not archive:
        return 0
    cutoff = _utcnow() - timedelta(hours=hours)
    count = 0
    for entry in archive:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("_retirement_reason") or "").startswith(
            "low_success_rate"
        ):
            continue
        ts_str = entry.get("_archived_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            count += 1
    return count
