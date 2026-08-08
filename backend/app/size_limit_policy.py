"""Canonical module size-limit bump policy — single source of truth.

When a justified change pushes a module over its line-count cap in
``backend/testing/module_size_limits.json``, the cap is bumped by a fixed
policy (never agent judgment). That policy lives here so the two consumers
agree byte-for-byte:

* the server-side ``/api/agent/annotate`` size_breach endpoint
  (``app.routers.admin_agent_ops._bump_size_limit``), and
* the standalone checker script
  (``backend/testing/check_size_limits.py``).

Stdlib-only and dependency-free (no DB, no network, no third-party imports)
so the checker can import it without any install step or runtime context.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict


def bump_size_limit(current_size: int) -> int:
    """Return the new cap for a file that breached its size limit.

    Policy: the larger of ``current_size + 200`` (a flat floor so a small
    overrun still gets reasonable headroom) and ``int(current_size * 1.15)``
    (15% proportional growth so a large file isn't left with tiny headroom),
    rounded UP to the nearest 50 lines for stability.
    """
    floor = current_size + 200
    proportional = int(current_size * 1.15)
    raw = max(floor, proportional)
    return ((raw + 49) // 50) * 50


def is_breach(count: int | None, cap: int | None) -> bool:
    """True when ``count`` breaches ``cap`` — the guardrail's fundamental predicate.

    A record is in breach only when BOTH ``count`` and ``cap`` are present AND
    ``count > cap``. A file absent at the ref (``count is None``) is never a
    breach — a missing file cannot have grown past its cap. Centralized here in
    the policy module (the documented single source of truth for the size
    guardrail) so the standalone checker, the CI-compliance evaluator, and any
    server-side classification agree on the comparison byte-for-byte instead of
    re-deriving it inline.
    """
    return count is not None and cap is not None and count > cap


# ---------------------------------------------------------------------------
# Chronic-offender governance
# ---------------------------------------------------------------------------

# A module is a chronic offender when it has been bumped at least this many
# times in the rolling window, OR its cap has grown by at least this fraction
# since the first recorded bump.
CHRONIC_BUMP_THRESHOLD: int = 3
CHRONIC_WINDOW_DAYS: int = 180
CHRONIC_GROWTH_PCT: float = 0.30


class BumpRecord(TypedDict):
    commit_sha: str
    commit_date: str  # ISO-8601
    old_cap: int
    new_cap: int


class BumpHistoryUnavailableError(Exception):
    """Raised when cap-bump history could not be read.

    Distinct from an empty history: ``[]`` means the read SUCCEEDED and
    found no cap bumps for the key, while this exception means the read itself
    failed (subprocess timeout, git not available, a non-zero ``git log`` exit
    in a complete checkout, or an unreadable/malformed ledger mirror). A
    truncated/shallow clone does NOT raise here: :func:`parse_bump_history`
    consults the committed ledger mirror (complete even when the clone is
    shallow) before failing closed. Callers that gate a cap bump on the
    chronic-offender verdict must treat this state as fail-closed (refuse the
    bump) rather than as "no bump history".
    """


# Module-level TTL cache: (module_key, section) -> (outcome, monotonic_time)
# where ``outcome`` is either a ``list[BumpRecord]`` (read succeeded) or a
# ``BumpHistoryUnavailableError`` (read failed). Failures are cached too so a
# broken-git reconcile cycle does not re-shell (and re-timeout) per call.
_bump_history_cache: dict[
    tuple[str, str], tuple[list[BumpRecord] | BumpHistoryUnavailableError, float]
] = {}
_CACHE_TTL_SECONDS: float = 60.0

# Repo root: backend/app/ -> ../../
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TESTING_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "testing"))
_MODULE_SIZE_LIMITS_RELPATH = "backend/testing/module_size_limits.json"
_MODULE_SIZE_LIMITS_PATH = os.path.join(_TESTING_DIR, "module_size_limits.json")

# Committed cap-bump-history ledger. In the deployed backend layout the git
# evidence source is structurally unreachable (the production ``app`` stage
# ships no git binary, and under WORKDIR /app ``_REPO_ROOT`` resolves to ``/``
# which is not a checkout — ``.dockerignore`` also excludes ``.git``), so
# :func:`parse_bump_history` reads this mirror of ``git log -p``'s bump records
# instead of failing closed on history it can never obtain. Resolved relative to
# this file so it works identically in a dev checkout (``backend/testing/``) and
# in the image (``/app/testing/``). Maintained by ``check_size_limits.py --fix``
# and the rollup size preflight; a guard test keeps it in sync with git.
_CAP_BUMP_HISTORY_LEDGER_PATH = os.path.join(_TESTING_DIR, "cap_bump_history.json")
_CAP_BUMP_HISTORY_LEDGER_SCHEMA = 1


def _git_reads_history_here() -> bool:
    """True only when git is on PATH AND ``_REPO_ROOT`` is a git checkout.

    These are the two structural preconditions the deployed backend layout
    fails: the production ``app`` stage has no git binary, and under WORKDIR
    /app ``_REPO_ROOT`` resolves to ``/`` (no ``.git``; the image also excludes
    it). When either is false the git evidence source is unreachable and
    :func:`parse_bump_history` reads the committed ledger mirror instead.
    """
    return shutil.which("git") is not None and os.path.isdir(
        os.path.join(_REPO_ROOT, ".git")
    )


# Ref scope for cap-bump history: the mainline integration branch's ancestry,
# NEVER ``--all``.  The ledger mirrors only mainline bumps — cap bumps recorded
# on abandoned task/research branches never merged and must not enter
# chronic-offender classification — so every git read of this history must be
# scoped to a single mainline branch.  Candidates are ordered to prefer the
# remote-tracking integration branch (deterministic across clones regardless of
# the checked-out local branch), then the local branch, then ``HEAD``.
_MAINLINE_BRANCH_CANDIDATES: tuple[str, ...] = (
    "origin/automain",
    "automain",
    "origin/main",
    "main",
)


def _mainline_git_ref(repo_root: str | None = None) -> str:
    """Resolve the ref whose ancestry is the cap-bump-history source of truth.

    Returns the first ``_MAINLINE_BRANCH_CANDIDATES`` ref that exists in the
    checkout, falling back to ``HEAD`` (the only ref guaranteed to resolve).
    ``HEAD`` is a fallback only: on a feature branch it scopes history to that
    branch's own ancestors, which can miss mainline bumps merged after the
    branch forked — the preferred candidates are the mainline branches.  All
    three call sites (the production read, the ``--fix`` regenerator, and the
    guard test) must use this same helper so the ledger and its checker cannot
    disagree by construction.
    """
    root = repo_root or _REPO_ROOT
    for candidate in _MAINLINE_BRANCH_CANDIDATES:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", candidate],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=root,
            )
        except Exception:  # noqa: BLE001 — a probe; unresolved ref falls through
            continue
        if result.returncode == 0:
            return candidate
    return "HEAD"


def _git_history_is_truncated(repo_root: str | None = None) -> bool:
    """True when the MAINLINE history source is truncated by a shallow boundary.

    A depth-limited clone (CI's depth-1 checkout, a task-VM shallow clone) has a
    ``.git/shallow`` boundary file and ``git log`` silently returns a partial
    history — treating that as complete would let the guard pass without
    evaluating it and let the chronic-offender classifier read a truncated bump
    history, both fail-open.  Callers never treat the partial history as
    complete: the production read consults the committed ledger mirror instead
    of git, and the ledger guard still evaluates the git ⊆ ledger direction
    (which partial history CAN prove) while skipping only the exact-equality
    reverse check.

    The presence of ``.git/shallow`` ALONE does not mean the mainline ancestry
    is truncated: the backend test harness runs ``git fetch --depth=1 origin
    testmon-cache``, which drops a shallow boundary on that unrelated branch
    while the mainline history stays complete.  Only when the mainline ref's
    ancestry actually crosses a shallow boundary is the source of truth
    truncated.  A boundary on an unrelated branch must not fail closed.
    """
    root = repo_root or _REPO_ROOT
    shallow_path = os.path.join(root, ".git", "shallow")
    if not os.path.isfile(shallow_path):
        return False
    try:
        with open(shallow_path, encoding="utf-8") as f:
            boundaries = [line.strip() for line in f if line.strip()]
    except OSError:
        # Unreadable boundary list — cannot prove the history is complete.
        return True
    if not boundaries:
        return False
    ref = _mainline_git_ref(root)
    for boundary in boundaries:
        try:
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", boundary, ref],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=root,
            )
        except Exception:  # noqa: BLE001 — probe; unresolvable boundary falls through
            continue
        if result.returncode == 0:
            # A shallow boundary commit is in the mainline ancestry, so the
            # mainline history is truncated.
            return True
    return False


def parse_bump_history(module_key: str, section: str = "backend") -> list[BumpRecord]:
    """Return the cap-bump history for ``module_key``.

    Reads ``backend/testing/module_size_limits.json``'s git history, extracts
    diff hunks commit-by-commit, and returns records where the given key's
    cap value increased (old_cap -> new_cap).  Results are sorted oldest-first.

    In the deployed backend layout — where git is structurally unavailable (no
    git binary and/or no git checkout under ``_REPO_ROOT``) — the history comes
    from the committed cap-bump-history ledger
    (``backend/testing/cap_bump_history.json``), which mirrors exactly what the
    git read would produce. This is what makes the chronic-offender guard's
    history-readable branch reachable in production.

    Cached for ``_CACHE_TTL_SECONDS`` so repeated calls in a single reconcile
    cycle do not re-shell.  Returns ``[]`` ONLY when the history read succeeded
    and contains no cap bumps for the key — a genuinely empty history.  Raises
    :class:`BumpHistoryUnavailableError` when the history could not be read at
    all (a real ``git log`` failure in a complete checkout, or an
    unreadable/malformed ledger in the deployed layout).  A truncated/shallow
    clone is NOT one of those cases: its ``git log`` would be silently partial,
    but the committed ledger mirror is complete, so it is consulted BEFORE
    failing closed.  Callers that gate the chronic-offender verdict on this
    history must distinguish "no bumps on record" from "history could not be
    read" instead of treating both as empty.
    """
    cache_key = (module_key, section)
    now = time.monotonic()
    cached = _bump_history_cache.get(cache_key)
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        outcome = cached[0]
        if isinstance(outcome, BumpHistoryUnavailableError):
            raise outcome
        return outcome

    records = None
    if _git_reads_history_here() and not _git_history_is_truncated():
        # Complete checkout: read the bump history from the mainline git
        # ancestry.  A real git failure here is genuine evidence loss and stays
        # fail-closed (raised, not degraded to "no bump history").
        try:
            records = _read_git_bump_history(module_key)
        except BumpHistoryUnavailableError as err:
            # Cache the failure too: a broken history read stays broken within
            # the TTL, so a reconcile cycle does not re-shell git per file.
            _bump_history_cache[cache_key] = (err, now)
            raise err
    if records is None:
        # git structurally unreachable (deployed layout) OR the mainline history
        # is truncated (a shallow clone's ``git log`` would be silently partial)
        # — read the committed ledger mirror of the same history.  The mirror is
        # complete in both cases, so a shallow checkout classifies from the same
        # history the deployed layout reads instead of failing closed on
        # evidence it could have obtained.
        try:
            records = _read_cap_bump_history_ledger(section, module_key)
        except BumpHistoryUnavailableError as err:
            _bump_history_cache[cache_key] = (err, now)
            raise err
    _bump_history_cache[cache_key] = (records, now)
    return records


def _read_git_bump_history(module_key: str) -> list[BumpRecord] | None:
    """Read bump history from git, or ``None`` when git is structurally absent.

    ``None`` (NOT an error) signals the deployed layout: no git binary on PATH
    or ``_REPO_ROOT`` is not a git checkout.  The caller then falls back to the
    committed ledger mirror.

    The history is read from the MAINLINE branch's ancestry
    (:func:`_mainline_git_ref`), never ``--all`` — bumps on abandoned
    task/research branches never merged and must not enter the chronic-offender
    classification.

    Any other failure to read git history — a subprocess error or a non-zero
    ``git log`` exit in a complete checkout — raises
    :class:`BumpHistoryUnavailableError`.  In a real checkout that is genuine
    evidence loss and must stay fail-closed rather than silently degrade to
    "no bump history".  (A shallow/truncated clone also raises here for a direct
    caller, but :func:`parse_bump_history` never reaches this function on a
    truncated checkout — it consults the committed ledger mirror first.)
    """
    if not _git_reads_history_here():
        return None
    if _git_history_is_truncated():
        raise BumpHistoryUnavailableError(
            f"git history is truncated (shallow checkout at {_REPO_ROOT}); "
            f"cannot read bump history for {module_key!r}"
        )
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                _mainline_git_ref(),
                "--format=COMMIT %H %aI",
                "-p",
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=_REPO_ROOT,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as BumpHistoryUnavailableError
        err = BumpHistoryUnavailableError(
            f"git log failed while reading bump history for {module_key!r}"
        )
        err.__cause__ = exc
        raise err
    if result.returncode != 0:
        raise BumpHistoryUnavailableError(
            f"git log exited {result.returncode} while reading bump history "
            f"for {module_key!r}"
        )
    return _parse_git_diff_output(result.stdout, module_key)


def _read_cap_bump_history_ledger(section: str, module_key: str) -> list[BumpRecord]:
    """Read cap-bump history for ``module_key`` from the committed ledger mirror.

    The ledger is the history source in the deployed backend layout, where git
    is structurally unreachable.  An unreadable or malformed ledger raises
    :class:`BumpHistoryUnavailableError` — the same fail-closed contract as an
    unreadable git read, so a missing/corrupt mirror can never silently degrade
    to "no bump history".  A valid ledger with no records for the key returns
    ``[]`` (genuinely no bumps on record).
    """
    try:
        with open(_CAP_BUMP_HISTORY_LEDGER_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        err = BumpHistoryUnavailableError(
            f"cap-bump-history ledger unreadable at "
            f"{_CAP_BUMP_HISTORY_LEDGER_PATH} while reading bump history for "
            f"{module_key!r}"
        )
        err.__cause__ = exc
        raise err

    history = data.get("history") if isinstance(data, dict) else None
    section_history = history.get(section) if isinstance(history, dict) else None
    raw_records = (
        section_history.get(module_key) if isinstance(section_history, dict) else None
    )
    if not raw_records:
        return []

    records: list[BumpRecord] = []
    for raw in raw_records:
        try:
            records.append(
                BumpRecord(
                    commit_sha=str(raw["commit_sha"]),
                    commit_date=str(raw["commit_date"]),
                    old_cap=int(raw["old_cap"]),
                    new_cap=int(raw["new_cap"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            err = BumpHistoryUnavailableError(
                f"malformed ledger record for {module_key!r} in "
                f"{_CAP_BUMP_HISTORY_LEDGER_PATH}: {raw!r}"
            )
            err.__cause__ = exc
            raise err
    # Sort by actual timestamp, matching _parse_git_diff_output — the ledger
    # stores records in that order, so a re-read must reproduce it even for
    # records whose mixed UTC offsets would reorder under a raw string sort.
    records.sort(key=lambda r: _parse_iso(r["commit_date"]))
    return records


def ledger_with_pending_bumps(
    ledger_data: dict, pending: list[tuple[str, str, int, int]]
) -> dict:
    """Return a copy of ``ledger_data`` with ``pending`` bumps appended.

    ``pending`` entries are ``(section, key, old_cap, new_cap)`` — cap bumps
    not yet in the committed history (an uncommitted ``--fix`` bump, or a rollup
    auto-bump about to be committed).  Records already present for the same
    (key, old_cap, new_cap) are not duplicated, so a retried rollup cycle (or a
    regenerated ``--fix``) is idempotent.
    """
    result = dict(ledger_data) if isinstance(ledger_data, dict) else {}
    history: dict = {}
    for section, section_data in (result.get("history") or {}).items():
        history[section] = dict(section_data) if isinstance(section_data, dict) else {}
    now_iso = datetime.now(timezone.utc).isoformat()
    for section, key, old_cap, new_cap in pending:
        section_records = history.setdefault(section, {})
        records = list(section_records.get(key) or [])
        known = {(r.get("old_cap"), r.get("new_cap")) for r in records}
        if (old_cap, new_cap) not in known:
            records.append(
                {
                    "commit_sha": "",
                    "commit_date": now_iso,
                    "old_cap": old_cap,
                    "new_cap": new_cap,
                }
            )
        section_records[key] = records
    result["schema_version"] = _CAP_BUMP_HISTORY_LEDGER_SCHEMA
    result["history"] = history
    return result


def rebuild_cap_bump_history_ledger(
    *,
    caps_json_path: str | None = None,
    ledger_path: str | None = None,
    pending: list[tuple[str, str, int, int]] | None = None,
) -> dict:
    """Regenerate the cap-bump-history ledger and write it; return the dict.

    The ledger mirrors the bump records ``git log -p -- module_size_limits.json``
    scoped to the MAINLINE branch's ancestry (:func:`_mainline_git_ref`) would
    produce, so the deployed backend (no git) classifies from the same history
    it would read in a checkout — and never from ``--all`` bumps on abandoned,
    never-merged branches.  Every cap-bump path must keep it in sync:
    ``check_size_limits.py --fix`` passes the bumps it applies as ``pending``,
    and the rollup size preflight appends its auto-bumps via
    :func:`ledger_with_pending_bumps` before committing both files together.

    When git is absent or the checkout is truncated (the mainline ancestry
    crosses a ``.git/shallow`` boundary — a shallow task-VM clone, CI's depth-1
    checkout — so ``git log`` would silently return a partial history), the
    existing ledger is preserved rather than regenerated — a truncated ``git
    log`` must never truncate the mirror.  In a complete checkout the mirror is
    rebuilt from the mainline ancestry, and only if that would not drop a
    record the existing ledger already holds for a current key is the mirror
    replaced outright (the merge path keeps both).  ``git log`` itself never
    runs on a truncated checkout.  ``caps_json_path`` / ``ledger_path``
    default to the shipped files (under ``backend/testing/``); callers with a
    hermetic fixture repo pass the fixture paths.
    """
    caps_path = caps_json_path or _MODULE_SIZE_LIMITS_PATH
    out_path = ledger_path or _CAP_BUMP_HISTORY_LEDGER_PATH

    try:
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        existing = {}
    existing_history = (
        existing.get("history") if isinstance(existing, dict) and existing.get("history") else {}
    )

    git_output = None
    if _git_reads_history_here() and not _git_history_is_truncated():
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    _mainline_git_ref(),
                    "--format=COMMIT %H %aI",
                    "-p",
                    "--",
                    _MODULE_SIZE_LIMITS_RELPATH,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=_REPO_ROOT,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to append-only below
            result = None
            _ = exc
        if result is not None and result.returncode == 0:
            git_output = result.stdout

    if git_output is not None:
        try:
            with open(caps_path, encoding="utf-8") as f:
                caps_data = json.load(f)
        except (OSError, ValueError):
            caps_data = {}
        caps_keys: dict[str, set[str]] = {}
        git_histories: dict[str, dict[str, list[BumpRecord]]] = {}
        for section, keys in caps_data.items():
            if not isinstance(keys, dict):
                continue
            caps_keys[section] = set(keys)
            section_records: dict[str, list[BumpRecord]] = {}
            for key in keys:
                recs = _parse_git_diff_output(git_output, key)
                if recs:
                    section_records[key] = recs
            if section_records:
                git_histories[section] = section_records

        if _git_would_drop_ledger_records(existing_history, git_histories, caps_keys):
            # git is truncated — merge rather than regenerate, so records already
            # in the mirror are never lost to a shallow ``git log``.
            base_history = _merge_bump_histories(existing_history, git_histories)
        else:
            base_history = git_histories
    else:
        # git entirely unavailable — preserve the existing mirror.
        base_history = existing_history

    ledger = {
        "_doc": (
            "Mirror of the cap-bump history recorded in "
            "backend/testing/module_size_limits.json's git log. The deployed "
            "backend has no git checkout, so classify_breach reads this file. "
            "Regenerate with check_size_limits.py --fix (or the rollup size "
            "preflight appends auto-bumps); a guard test keeps it in sync."
        ),
        "schema_version": _CAP_BUMP_HISTORY_LEDGER_SCHEMA,
        "history": base_history,
    }
    ledger = ledger_with_pending_bumps(ledger, pending or [])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=True)
        f.write("\n")
    return ledger


def _git_would_drop_ledger_records(
    existing_history: dict,
    git_histories: dict[str, dict[str, list[BumpRecord]]],
    caps_keys: dict[str, set[str]],
) -> bool:
    """True when regenerating from ``git_histories`` would drop a ledger record.

    Only current-caps keys are consulted (removed keys can no longer breach, so
    their records may be dropped safely).  If the existing ledger holds a record
    for a current key that the readable git history does not show, the mirror
    must be merged, not regenerated, or history would be silently lost.  The
    primary truncated-history guard is :func:`_git_history_is_truncated` (git is
    not even run on a shallow checkout); this is the secondary safety net for a
    checkout that is not marked shallow yet still lacks a ledger record.
    """
    for section, section_records in existing_history.items():
        if not isinstance(section_records, dict):
            continue
        current_keys = caps_keys.get(section, set())
        git_section = git_histories.get(section, {})
        for key, records in section_records.items():
            if key not in current_keys or not isinstance(records, list):
                continue
            git_pairs = {
                (r["old_cap"], r["new_cap"]) for r in git_section.get(key, [])
            }
            for record in records:
                try:
                    pair = (record["old_cap"], record["new_cap"])
                except (KeyError, TypeError):
                    return True
                if pair not in git_pairs:
                    return True
    return False


def _merge_bump_histories(
    existing_history: dict, git_histories: dict[str, dict[str, list[BumpRecord]]]
) -> dict[str, dict[str, list[BumpRecord]]]:
    """Union two section->key->records maps, deduped by (key, old_cap, new_cap).

    Existing records win on ties so the mirror's metadata is preserved; any
    git-visible record the mirror lacks is added.
    """
    merged: dict[str, dict[str, list[BumpRecord]]] = {}
    for section, section_records in existing_history.items():
        if not isinstance(section_records, dict):
            continue
        by_key: dict[str, dict] = {}
        for key, records in section_records.items():
            if not isinstance(records, list):
                continue
            by_key[key] = {
                (r.get("old_cap"), r.get("new_cap")): r for r in records
            }
        git_section = git_histories.get(section, {})
        for key, records in git_section.items():
            existing = by_key.setdefault(key, {})
            for r in records:
                existing.setdefault((r["old_cap"], r["new_cap"]), r)
        merged[section] = {
            key: sorted(records.values(), key=lambda r: _parse_iso(r["commit_date"]))
            for key, records in by_key.items()
            if records
        }
    return {s: sec for s, sec in merged.items() if sec}


def _parse_git_diff_output(output: str, module_key: str) -> list[BumpRecord]:
    """Parse ``git log -p`` output for cap increases of ``module_key``.

    Splits the output on "COMMIT <sha> <date>" header lines, then within each
    commit's diff looks for ``-    "<key>": <int>`` (removed) and
    ``+    "<key>": <int>`` (added) lines.  Records only increases
    (new_cap > old_cap) — a decrease or reset is not a bump.

    Merge-duplicated bump pairs are collapsed: the same cap change
    (old_cap -> new_cap) is frequently recorded twice in ``git log --all`` —
    once on the commit that first introduced the new limit value (typically an
    integration-branch rollup) and again on a sibling/promotion commit that
    re-applied the identical value (e.g. a main-side "adopt automain" fix).
    Both diffs carry the exact same -/+ pair, so a naive parse would count one
    logical bump twice and inflate the chronic-offender window/growth triggers.
    Rule: for each (old_cap, new_cap) pair keep ONLY the record with the
    earliest commit_date — the commit that first introduced the new limit
    value — and drop the later duplicate(s).  Records are sorted by actual
    timestamp (not the ISO string, which mis-orders mixed-offset commits).
    This keeps regeneration stable and idempotent: the surviving records depend
    only on the content of the history, not on which refs happen to be fetched
    or on git's traversal order.
    """
    records: list[BumpRecord] = []
    # Matches +/- diff lines containing the key and a numeric cap value.
    cap_re = re.compile(rf'^([+-])\s+"{re.escape(module_key)}":\s*(\d+)', re.MULTILINE)

    # Accumulate (sha, date, diff_text) for each commit.
    chunks: list[tuple[str, str, str]] = []
    current_sha = ""
    current_date = ""
    current_lines: list[str] = []

    for line in output.split("\n"):
        if line.startswith("COMMIT "):
            if current_sha:
                chunks.append((current_sha, current_date, "\n".join(current_lines)))
            parts = line.split(" ", 2)
            current_sha = parts[1] if len(parts) > 1 else ""
            current_date = parts[2].strip() if len(parts) > 2 else ""
            current_lines = []
        else:
            current_lines.append(line)

    if current_sha:
        chunks.append((current_sha, current_date, "\n".join(current_lines)))

    for sha, date, diff_text in chunks:
        old_cap: int | None = None
        new_cap: int | None = None
        for m in cap_re.finditer(diff_text):
            sign, val_str = m.group(1), m.group(2)
            val = int(val_str)
            if sign == "-":
                old_cap = val
            elif sign == "+":
                new_cap = val
        if old_cap is not None and new_cap is not None and new_cap > old_cap:
            records.append(
                BumpRecord(
                    commit_sha=sha,
                    commit_date=date,
                    old_cap=old_cap,
                    new_cap=new_cap,
                )
            )

    # Collapse merge-duplicated bump pairs to the earliest-dated survivor (see
    # the docstring for the rule). git log emits commits newest-first, so the
    # sibling/promotion duplicate of a bump appears BEFORE the commit that
    # introduced it — the survivor must be chosen by timestamp, not position.
    earliest_by_pair: dict[tuple[int, int], BumpRecord] = {}
    for record in records:
        pair = (record["old_cap"], record["new_cap"])
        prior = earliest_by_pair.get(pair)
        if prior is None or _parse_iso(record["commit_date"]) < _parse_iso(
            prior["commit_date"]
        ):
            earliest_by_pair[pair] = record
    return sorted(
        earliest_by_pair.values(),
        key=lambda r: _parse_iso(r["commit_date"]),
    )


def _is_chronic_offender(history: list[BumpRecord]) -> bool:
    """Return True when ``history`` meets the chronic-offender threshold.

    Two triggers — either fires the classification:

    1. **Window check**: at least ``CHRONIC_BUMP_THRESHOLD`` bumps within the
       last ``CHRONIC_WINDOW_DAYS`` days.
    2. **Cumulative-growth check**: total cap growth from the first recorded
       ``old_cap`` to the last recorded ``new_cap`` is >= ``CHRONIC_GROWTH_PCT``
       AND there are at least ``CHRONIC_BUMP_THRESHOLD`` bumps on record
       (avoids false-positives from a single jump on a fresh file).
    """
    if len(history) < CHRONIC_BUMP_THRESHOLD:
        return False

    # Trigger 1 — window check.
    cutoff = datetime.now(timezone.utc) - timedelta(days=CHRONIC_WINDOW_DAYS)
    recent_count = sum(1 for r in history if _parse_iso(r["commit_date"]) >= cutoff)
    if recent_count >= CHRONIC_BUMP_THRESHOLD:
        return True

    # Trigger 2 — cumulative growth check.
    first_cap = history[0]["old_cap"]
    current_cap = history[-1]["new_cap"]
    return first_cap > 0 and (current_cap - first_cap) / first_cap >= CHRONIC_GROWTH_PCT


class BreachVerdict(TypedDict):
    is_chronic: bool
    suggested_new_limit: int | None  # None when chronic and no operator override
    bump_count: int
    reason: str
    history_unavailable: bool  # True when bump history could not be read


def classify_breach(
    module_key: str,
    current_size: int,
    section: str = "backend",
    *,
    override_chronic: bool = False,
) -> BreachVerdict:
    """Classify a size breach as routine or chronic-offender escalation.

    Returns a ``BreachVerdict`` with ``is_chronic=True`` and
    ``suggested_new_limit=None`` for chronic offenders (unless
    ``override_chronic=True``, the explicit operator-approval path, which
    grants the bump but records attribution).

    When bump history could not be read, the verdict FAILS CLOSED:
    ``is_chronic=True`` with ``suggested_new_limit=None`` and
    ``history_unavailable=True``, routing the breach to decomposition rather
    than silently granting a routine cap bump.  This happens only when BOTH
    sources are unusable: a genuine ``git log`` failure in a complete checkout
    OR an unreadable/malformed committed ledger mirror.  A truncated/shallow
    clone is NOT one of those cases — :func:`parse_bump_history` reads the
    committed ledger mirror (complete even on a shallow checkout), so the
    verdict classifies from the same history the deployed layout reads.  A
    genuinely readable but empty history keeps the routine-bump behaviour — the
    guard distinguishes "no bumps on record" from "history could not be read"
    instead of treating both as empty.
    """
    try:
        history = parse_bump_history(module_key, section)
    except BumpHistoryUnavailableError as exc:
        # Unreadable history is NOT "no bumps on record": refuse the automatic
        # bump and route to decomposition. An explicit operator override
        # (override_chronic=True) still grants the bump — it is the documented
        # human-approval escape hatch, identical to a verified chronic offender.
        if override_chronic:
            return BreachVerdict(
                is_chronic=True,
                suggested_new_limit=bump_size_limit(current_size),
                bump_count=0,
                history_unavailable=True,
                reason=(
                    f"Bump history could not be read: {exc}. "
                    "Operator-approved bump granted despite unreadable history."
                ),
            )
        return BreachVerdict(
            is_chronic=True,
            suggested_new_limit=None,
            bump_count=0,
            history_unavailable=True,
            reason=(
                f"Bump history could not be read: {exc}. "
                "Refusing cap bump — decomposition required until history is readable."
            ),
        )

    chronic = _is_chronic_offender(history)
    bump_count = len(history)

    if chronic and not override_chronic:
        return BreachVerdict(
            is_chronic=True,
            suggested_new_limit=None,
            bump_count=bump_count,
            history_unavailable=False,
            reason=(
                f"Chronic offender: {bump_count} cap bumps on record. "
                "Decomposition required — do not bump without operator approval."
            ),
        )

    return BreachVerdict(
        is_chronic=chronic,
        suggested_new_limit=bump_size_limit(current_size),
        bump_count=bump_count,
        history_unavailable=False,
        reason=(
            "Chronic offender — operator-approved bump."
            if chronic
            else f"Routine breach ({bump_count} prior bumps)."
        ),
    )


def _parse_iso(date_str: str) -> datetime:
    """Parse an ISO-8601 datetime string to a tz-aware datetime.

    ``datetime.fromisoformat`` in Python 3.11+ handles the full ISO-8601
    format including offsets; older versions need a minor workaround for the
    ``+HH:MM`` suffix.
    """
    try:
        dt = datetime.fromisoformat(date_str)
    except ValueError:
        # Fallback: strip the timezone suffix and assume UTC.
        dt = datetime.fromisoformat(date_str[:19]).replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
