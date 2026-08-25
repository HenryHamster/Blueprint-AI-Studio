#!/usr/bin/env python3
"""Fast, dependency-free module size-guardrail checker.

Mirrors the line-count guardrail enforced by ``backend/testing/test_invariants.py``
(``module_size_limits.json``) so an agent can surface and remediate size breaches
*before* opening a PR — without spinning up Docker, a DB, or the full test suite.

Two modes:

* check mode (default): line-count every file listed in the scoped section(s) and
  print each breach (path, current lines, cap). Exits non-zero if any file exceeds
  its cap, zero when clean. Check mode ALSO guards the ratchet itself: a cap
  raised above the MAINLINE ancestry tip (``_mainline_git_ref``) with no
  cap_bump_history.json entry justifying the exact (old -> new) raise is a SILENT
  cap bump and fails the check, naming the file, the old and new values, and how
  to record the bump. Lowering a cap is always legal and never needs a ledger
  entry. When the mainline baseline cannot be established (not a git checkout, no
  mainline branch, unreadable file at the ref — e.g. a shallow CI clone that never
  fetched the integration branch) the guard degrades to a clear skip, never a
  crash or a spurious failure. Check mode ALSO verifies the reverse direction of
  the ledger contract: every cap_bump_history.json record must be justified by
  the MAINLINE git history (a real increase of the exact old -> new caps, made by
  a commit in the mainline ancestry). A ledger record with a wrong old_cap, a
  decrease mis-filed as a bump, a commit outside the mainline ancestry, or an
  unresolvable SHA is a PHANTOM record and fails the check as ``[ledger-phantom]``.
  Unlike the cap-raise baseline this runs on truncated/shallow checkouts too —
  every CI shard and agent VM — so phantom drift is caught where it accrues. When
  git history is unavailable the guard degrades to a clear skip.
* ``--fix`` mode: for every breached file, rewrite *only* that file's cap in the
  tracked JSON to the canonical repair policy value; governed modules also have
  their declared source constant rewritten in the same repair; AND for every
  uncapped source file over ``_COVERAGE_THRESHOLD`` lines (the complementary
  coverage guardrail the ``test_*_no_uncapped_large_file`` meta-tests enforce),
  ADD a new cap entry sized at the same policy headroom. Each applied bump is
  appended to the cap_bump_history.json ledger, so the very raise ``--fix`` makes
  is the justification a follow-up check accepts. Both paths preserve key order
  and the leading ``_doc`` field so the diff stays minimal. After ``--fix``
  re-running check mode passes. The bump stays policy-consistent so the guardrail
  still catches genuine runaway growth. ``--fix`` ALSO self-heals the ledger: any
  record the mainline git history cannot justify is pruned by rebuilding
  cap_bump_history.json from git, even when no cap breach exists to bump.

Usage (from the repo root, no install step)::

    python3 backend/testing/check_size_limits.py                 # check all sections
    python3 backend/testing/check_size_limits.py --sections backend
    python3 backend/testing/check_size_limits.py --fix           # bump breached caps
    python3 backend/testing/check_size_limits.py --fix --repair-target agent/prompt.ts
                                  # repair only an attributed current offender
    python3 backend/testing/check_size_limits.py --base-root /tmp/base_wt
                                  # baseline-aware: block only NEW/GROWN offenders

Stdlib only. The five sections resolve their source roots exactly the way
``test_invariants.py`` does: backend -> ``backend/app``, agent -> ``agent/src``,
frontend -> ``admin-ui/src`` (honoring the ``ADMIN_UI_SRC`` override and the
Docker ``/admin-ui/src`` mount), testing -> ``backend/testing``, and
scripts -> ``scripts``. A section whose source tree is absent in the current
environment is reported as skipped rather than treated as an error.
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys

# Make ``app.size_limit_policy`` importable when run as
# ``python3 backend/testing/check_size_limits.py`` from the repo root: add the
# backend dir (this file's parent's parent) to sys.path. The policy itself is
# imported lazily (in ``_bump_size_limit``) so this path setup runs first.
_BACKEND_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
# The shared receipt writers live in the repo's ``scripts/`` dir, which is NOT
# a sibling of ``_BACKEND_DIR`` in every environment: the Docker test image
# carries the backend tree as ``/app`` with no ``scripts/`` sibling and mounts
# the repo root at ``/repo``. The old fixed ``<_BACKEND_DIR>/../scripts`` walk
# resolved to a nonexistent ``/scripts`` there, so every receipt import failed
# with nothing naming why. Probe both layouts, and keep the miss REASON so the
# fail-open receipt warnings below can report it instead of a bare ImportError.
from testing.repo_scripts_path import resolve_scripts_dir_from_env

_SCRIPTS_DIR, _SCRIPTS_DIR_UNAVAILABLE = resolve_scripts_dir_from_env(_BACKEND_DIR)
_SCRIPTS_DIR_HINT = f" — {_SCRIPTS_DIR_UNAVAILABLE}" if _SCRIPTS_DIR_UNAVAILABLE else ""
if _SCRIPTS_DIR is not None and _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

try:  # absent from the backend-only Docker context, which has no scripts/
    from utf8_io import force_utf8_streams
except ImportError:  # pragma: no cover - that context is a UTF-8 host already
    def force_utf8_streams() -> None:
        return None

# Inserting ``_BACKEND_DIR`` at the front of ``sys.path`` only wins the
# ``app.size_limit_policy`` import when ``<_BACKEND_DIR>/app`` is a REGULAR
# package. If it is a bare directory with no ``__init__.py`` (e.g. a stripped
# test fixture checkout), PEP 420 namespace-package resolution does not stop
# there — it keeps walking the rest of ``sys.path`` and can silently bind
# ``app.*`` to an unrelated ``app`` package elsewhere (for example a real repo
# checkout reachable via ``PYTHONPATH``), so this checker would read that
# other tree's ``module_size_limits.json`` and cap-bump ledger instead of its
# own. Fail closed instead of risking a silent misattribution.
_APP_INIT_PATH = os.path.join(_BACKEND_DIR, "app", "__init__.py")
if not os.path.isfile(_APP_INIT_PATH):
    raise RuntimeError(
        f"{os.path.join(_BACKEND_DIR, 'app')} is not a regular Python package "
        f"(missing {_APP_INIT_PATH}) — refusing to import app.size_limit_policy "
        "because PEP 420 namespace-package resolution could silently bind it "
        "to an unrelated `app` package elsewhere on sys.path"
    )


def _bump_size_limit(
    current_size: int,
    module_key: str | None = None,
    section: str = "backend",
    repo_root: str | None = None,
) -> int:
    """Canonical bump policy, shared with the server endpoint.

    Imported lazily so the ``sys.path`` setup above runs before the import and
    the module stays runnable as a bare script from the repo root."""
    from app.size_limit_policy import bump_size_limit

    return bump_size_limit(current_size, module_key, section, repo_root)


def recommended_cap_for_line_count(
    current_size: int,
    module_key: str | None = None,
    section: str = "backend",
    repo_root: str | None = None,
) -> int:
    """Return the canonical cap for a newly tracked source file.

    Keeping the uncapped-file path behind this named policy boundary makes the
    cap chosen for a newly extracted module explicit and directly testable.
    It intentionally delegates to the same bump policy used for existing
    breaches, so adding a cap cannot create a second headroom rule.
    """
    return _bump_size_limit(current_size, module_key, section, repo_root)


def _classify_breach(
    module_key: str,
    current_size: int,
    section: str = "backend",
    *,
    override_chronic: bool = False,
    repo_root: str | None = None,
):
    """Chronic-offender-aware breach verdict, shared with the server endpoint.

    Imported lazily for the same reason as ``_bump_size_limit`` above."""
    from app.size_limit_policy import classify_breach

    return classify_breach(
        module_key,
        current_size,
        section,
        override_chronic=override_chronic,
        repo_root=repo_root,
    )


def classify_size_record(
    rel: str,
    count: "int | None",
    cap: "int | None",
    section: str = "backend",
    *,
    override_chronic: bool = False,
    with_verdict: bool = True,
    repo_root: str | None = None,
    governance_caps: "list[int] | None" = None,
) -> dict:
    """Content-injectable breach-and-bump classification for ONE counted record.

    Pure / filesystem-free: the caller supplies the already-computed line
    ``count`` (``None`` when the file is absent / unreadable) and the ``cap``.
    This is the SINGLE source of truth for the breach test (``count > cap``) and
    the chronic/bump policy (``_classify_breach``), so the disk-mode CLI path
    (via :func:`scan_section`) and the at-SHA evaluator
    (``backend/app/ci_compliance.py``) classify identically without duplicating
    the policy.

    Returns ``{rel, count, cap, breach, verdict}``:

      * ``breach`` — True only when BOTH ``count`` and ``cap`` are present and
        ``count > cap`` (delegated to ``app.size_limit_policy.is_breach``, the
        single source of truth for the comparison). A file absent at the ref
        (``count is None``) is never a breach (a missing file cannot have grown
        past its cap).
      * ``verdict`` — the ``BreachVerdict`` from :func:`_classify_breach` when
        ``breach`` (and ``with_verdict``), else ``None``. ``with_verdict=False``
        skips the (git-touching) chronic lookup for the plain check path, which
        only needs the breach flag.

    ``governance_caps``, passed by the at-SHA evaluator with the cap parsed from
    the same ref text it fetched, overrides the local-tree lookup (see
    ``resolve_effective_cap``).
    """
    from app.size_limit_policy import is_breach, resolve_effective_cap

    effective_cap = resolve_effective_cap(rel, section, cap, repo_root=repo_root, governance_caps=governance_caps)
    breach = is_breach(count, effective_cap)
    verdict = None
    if breach and with_verdict:
        verdict = _classify_breach(
            rel,
            count,
            section,
            override_chronic=override_chronic,
            repo_root=repo_root,
        )
    return {
        "rel": rel,
        "count": count,
        "cap": cap,
        "effective_cap": effective_cap,
        "breach": breach,
        "verdict": verdict,
    }


# Repo root is two levels up from this file (backend/testing -> repo root).
_DEFAULT_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_DEFAULT_JSON_PATH = os.path.join(os.path.dirname(__file__), "module_size_limits.json")


def _is_git_checkout(repo_root: str) -> bool:
    """Recognize both normal checkouts and linked worktrees."""
    git_marker = os.path.join(repo_root, ".git")
    return os.path.isdir(git_marker) or os.path.isfile(git_marker)

# Section order is significant: matches the JSON's section order so output and
# --sections=all iteration are deterministic.
SECTIONS = ("backend", "agent", "frontend", "testing", "scripts")

# The release/verification surface this guardrail was built to bound: every
# backend/app/release_*.py and acceptance_*.py module, plus
# approved_pr_stranding.py. A module on this surface must declare its own
# ``*_SIZE_GOVERNANCE_CAP`` — that's what makes the ratchet in
# ``resolve_effective_cap`` bind. A new module added here with no declaration
# is exactly how the surface reopened after growth was first bounded: it adds
# a file rather than growing an existing one, so the per-file caps never see
# it.
# --force-chronic is the operator-approval bypass for the chronic-offender
# refusal. It ran unguarded until an autonomous auto-fix task used it to raise
# ci_completion.py 3100 -> 3600 on its seventh bump (PR #17587, reverted). The
# flag now demands this variable so the approval has to come from outside the
# task: an agent that hits a chronic offender must decompose it or block.
_OPERATOR_APPROVAL_ENV = "SIZE_CAP_OPERATOR_APPROVAL"

_RELEASE_SURFACE_PREFIXES = ("release_", "acceptance_")
_RELEASE_SURFACE_EXTRA_MODULES = ("approved_pr_stranding.py",)


def find_ungoverned_release_surface_modules(repo_root: str) -> list[str]:
    """Release-surface modules with no declared governance cap.

    Returns sorted module filenames (relative to ``backend/app``). Empty when
    the backend source tree isn't present in this environment.
    """
    from app.size_limit_policy import resolve_effective_cap

    root = os.path.join(repo_root, "backend", "app")
    if not os.path.isdir(root):
        return []
    missing = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        if not (
            name.startswith(_RELEASE_SURFACE_PREFIXES)
            or name in _RELEASE_SURFACE_EXTRA_MODULES
        ):
            continue
        if resolve_effective_cap(name, "backend", None, repo_root=repo_root) is None:
            missing.append(name)
    return missing

# Coverage threshold for the complementary guardrail: a source file larger than
# this with NO cap entry at all is invisible to the per-file caps and can grow
# without limit. Mirrors ``test_invariants._COVERAGE_THRESHOLD`` (and the agent /
# frontend coverage meta-tests). Kept in sync by a guard test in
# test_size_limits_checker.py. NEVER mirror this by importing test_invariants —
# that module pulls in pytest/app machinery and would break the stdlib-only,
# dependency-free contract this checker guarantees.
_COVERAGE_THRESHOLD = 1500


def _is_frontend_excluded(rel_path: str) -> bool:
    """Mirror test_invariants._is_frontend_excluded: skip frontend test/type files."""
    return rel_path.endswith((".test.ts", ".test.tsx", ".d.ts")) or rel_path.startswith(
        "test/"
    )


def _is_agent_excluded(rel_path: str) -> bool:
    """Mirror test_invariants._is_agent_excluded: skip agent test/type files."""
    return rel_path.endswith((".test.ts", ".d.ts"))


def _no_exclusion(_rel_path: str) -> bool:
    return False


# Per-section scan rules for the uncapped-file coverage guardrail. Mirrors the
# suffix tuples and exclusion predicates the coverage meta-tests in
# test_invariants.py use, so --fix adds the exact caps those tests demand.
_SECTION_SCAN = {
    "backend": ((".py",), _no_exclusion),
    "agent": ((".ts",), _is_agent_excluded),
    "frontend": ((".ts", ".tsx"), _is_frontend_excluded),
    "testing": ((".py",), _no_exclusion),
    "scripts": ((".py",), _no_exclusion),
}


def resolve_section_root(
    section: str, repo_root: str, *, use_env_overrides: bool = True
) -> str | None:
    """Resolve a section's source tree root, mirroring test_invariants.py.

    Returns the directory path if it exists, else None (caller skips the
    section). frontend honors the ``ADMIN_UI_SRC`` override and the Docker
    ``/admin-ui/src`` mount before falling back to ``<repo>/admin-ui/src``.

    ``use_env_overrides`` gates the environment-specific frontend escape
    hatches (the ``ADMIN_UI_SRC`` env var and the global ``/admin-ui/src``
    Docker mount). They make sense only for the *real* run rooted at the
    default repo root; when an explicit ``--repo-root`` is supplied (e.g. a
    hermetic test fixture) resolution must stay strictly under that root so a
    stray global mount does not leak the host environment into the result.
    """
    if section == "backend":
        root = os.path.join(repo_root, "backend", "app")
        return root if os.path.isdir(root) else None
    if section == "agent":
        root = os.path.join(repo_root, "agent", "src")
        return root if os.path.isdir(root) else None
    if section == "frontend":
        candidates = []
        if use_env_overrides:
            env_override = os.getenv("ADMIN_UI_SRC")
            if env_override:
                candidates.append(env_override)
            candidates.append("/admin-ui/src")
        candidates.append(os.path.join(repo_root, "admin-ui", "src"))
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate
        return None
    if section == "testing":
        root = os.path.join(repo_root, "backend", "testing")
        return root if os.path.isdir(root) else None
    if section == "scripts":
        root = os.path.join(repo_root, "scripts")
        return root if os.path.isdir(root) else None
    raise ValueError(f"Unknown section: {section}")


def count_lines(path: str) -> int:
    """Line-count a file the same way test_invariants.py does.

    The line count is encoding-invariant (only the newline byte matters), so
    ``errors="replace"`` yields the identical count to test_invariants.py's
    default ``open()`` for our utf-8 sources. ``errors="replace"`` (not a bare
    utf-8 pin) is deliberate: this checker runs on EVERY ./check now, so a stray
    non-utf-8 file must NOT raise UnicodeDecodeError and hard-fail every agent's
    gate fleet-wide — it degrades (still counts lines correctly) and any real
    encoding problem still surfaces via the actual test suite.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        return len(f.readlines())


def iter_capped_paths(root: str, caps: dict) -> "list[tuple[str, str, int | None]]":
    """Return deterministic ``(relative path, absolute path, cap)`` records.

    The CI compliance gate and the command-line scanner both consume the same
    cap mapping. Keeping path resolution in this dependency-free module makes
    a newly registered testing companion (such as an extracted test seam)
    visible to both checks without duplicating the JSON traversal contract.
    Missing files are retained so callers that diagnose stale caps preserve the
    existing ``count=None`` behavior.
    """
    return [(rel, os.path.join(root, rel), cap) for rel, cap in caps.items()]


def scan_section(
    caps: dict,
    root: str,
    section: str = "backend",
    *,
    repo_root: str | None = None,
) -> list[dict]:
    """Line-count every file in ``caps`` under ``root``.

    Returns one record per file: ``{rel, path, count, cap, breach, verdict}``.
    A file missing from disk is recorded with ``count=None`` and
    ``breach=False`` (a missing file can't have grown past its cap).

    The breach test is delegated to :func:`classify_size_record` (the shared,
    filesystem-free policy) so the disk-mode CLI and the at-SHA evaluator agree
    bit-for-bit. ``with_verdict=False`` keeps the plain check path free of the
    per-file git lookup the chronic verdict would trigger.
    """
    records = []
    for rel, path, cap in iter_capped_paths(root, caps):
        count = count_lines(path) if os.path.isfile(path) else None
        rec = classify_size_record(
            rel,
            count,
            cap,
            section,
            with_verdict=False,
            repo_root=repo_root,
        )
        rec["path"] = path
        records.append(rec)
    return records


def scan_uncapped_section(
    root: str,
    caps: dict,
    suffixes: tuple,
    is_excluded,
    *,
    section: str | None = None,
    repo_root: str | None = None,
) -> list[dict]:
    """Walk ``root`` for source files over ``_COVERAGE_THRESHOLD`` lines that have
    NO cap entry in ``caps``.

    Mirrors ``test_invariants._scan_uncapped``: collects every file whose name
    ends in one of ``suffixes``, is not skipped by ``is_excluded``, exceeds the
    coverage threshold, and is absent from ``caps``. Returns one record per file
    (``{rel, path, count}``) sorted largest-first. Tree-relative paths use forward
    slashes to match the JSON key convention.

    A file that declares its own ``*_SIZE_GOVERNANCE_CAP`` is excluded here
    when ``section``/``repo_root`` are supplied: it is enforced by
    :func:`scan_governed_missing_cap` at its declared cap instead, which may
    be well under the generic coverage threshold. Omitting ``section``/
    ``repo_root`` (the default) preserves the plain threshold-only scan for
    callers that don't need governance awareness.
    """
    from app.size_limit_policy import is_breach, resolve_effective_cap

    offenders = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(suffixes):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if is_excluded(rel) or rel in caps:
                continue
            if section is not None and (
                resolve_effective_cap(rel, section, None, repo_root=repo_root)
                is not None
            ):
                continue
            count = count_lines(abs_path)
            if is_breach(count, _COVERAGE_THRESHOLD):
                offenders.append({"rel": rel, "path": abs_path, "count": count})
    offenders.sort(key=lambda rec: rec["count"], reverse=True)
    return offenders


def scan_governed_missing_cap(
    root: str, caps: dict, section: str, *, repo_root: str | None = None
) -> list[dict]:
    """Governed modules with NO JSON cap entry, enforced at their own cap.

    A module that declares ``*_SIZE_GOVERNANCE_CAP`` but has no
    ``module_size_limits.json`` entry is invisible to :func:`scan_section`
    (which only walks JSON-listed files) and, below the generic coverage
    threshold, also invisible to :func:`scan_uncapped_section`. This closes
    that gap: every file in ``root`` that declares a governance cap and has
    no JSON entry is classified against that cap directly, using the same
    :func:`classify_size_record` breach test as every other record so
    ``evaluate`` reports it uniformly.
    """
    suffixes, is_excluded = _SECTION_SCAN[section]
    records = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(suffixes):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if is_excluded(rel) or rel in caps:
                continue
            from app.size_limit_policy import resolve_effective_cap

            governance_cap = resolve_effective_cap(
                rel, section, None, repo_root=repo_root
            )
            if governance_cap is None:
                continue
            count = count_lines(abs_path)
            rec = classify_size_record(
                rel,
                count,
                governance_cap,
                section,
                with_verdict=False,
                repo_root=repo_root,
            )
            rec["path"] = abs_path
            records.append(rec)
    return records


def load_limits(json_path: str) -> dict:
    """Load the size-limits JSON, preserving key order (dicts are ordered).

    Strict utf-8, NOT ``errors="replace"`` like :func:`count_lines`: ``--fix``
    writes back what it read, so replacing here would launder mojibake into the
    committed ledger. An undecodable ledger must fail loudly instead.
    """
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)


def write_limits(json_path: str, data: dict) -> None:
    """Write the size-limits JSON back, preserving structure and key order.

    Uses the same formatting as the tracked file (2-space indent, ASCII-escaped,
    trailing newline) so a --fix diff shows only the changed cap numbers.

    ``newline=""`` stops a Windows ``--fix`` rewriting every line of this ledger
    as CRLF -- a whole-file diff that defeats its ``sizecap`` merge driver.
    """
    with open(json_path, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")


def _snapshot_artifacts(paths: list[str]) -> dict[str, bytes | None]:
    """Capture repair artifacts so a failed ledger write can roll them back."""
    snapshots: dict[str, bytes | None] = {}
    for path in paths:
        if os.path.isfile(path):
            with open(path, "rb") as artifact:
                snapshots[path] = artifact.read()
        else:
            snapshots[path] = None
    return snapshots


def _restore_artifacts(snapshots: dict[str, bytes | None]) -> None:
    """Restore a pre-repair artifact set, including files created mid-repair."""
    for path, original in snapshots.items():
        if original is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            continue
        with open(path, "wb") as artifact:
            artifact.write(original)


def evaluate(
    data: dict, sections: list[str], repo_root: str, *, use_env_overrides: bool = True
) -> tuple[dict[str, list[dict]], list[str]]:
    """Evaluate the scoped sections.

    Returns ``(checked, skipped)`` where ``checked`` maps section -> list of
    file records (from scan_section) and ``skipped`` is the list of section
    names whose source tree was absent. ``use_env_overrides`` is forwarded to
    ``resolve_section_root`` (see its docstring).
    """
    checked: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for section in sections:
        caps = data.get(section) or {}
        root = resolve_section_root(
            section, repo_root, use_env_overrides=use_env_overrides
        )
        if root is None:
            skipped.append(section)
            continue
        records = scan_section(caps, root, section, repo_root=repo_root)
        records.extend(
            scan_governed_missing_cap(root, caps, section, repo_root=repo_root)
        )
        checked[section] = records
    return checked, skipped


def _collect_baseline_offenders(
    base_root: str, sections: list[str]
) -> "tuple[dict, dict, str | None]":
    """Offender maps for a BASE-branch checkout, or why the baseline is unusable.

    Returns ``(breached, uncapped, unavailable_reason)``. ``breached`` maps
    ``(section, rel) -> line count`` for every file already over its cap on the
    base checkout — judged against the BASE's own ``module_size_limits.json``
    (the caps this change inherited), not the PR's. ``uncapped`` maps
    ``(section, rel) -> line count`` for every base source file over the
    coverage threshold with no cap entry there. When the base checkout has no
    readable limits JSON the baseline cannot be computed: both maps come back
    empty with a reason, so the caller fails CLOSED (every PR-side offender is
    treated as newly introduced).

    Resolution is hermetic (``use_env_overrides=False``): the ADMIN_UI_SRC /
    ``/admin-ui/src`` escape hatches would leak the PR tree into the baseline.
    """
    base_json = os.path.join(base_root, "backend", "testing", "module_size_limits.json")
    try:
        base_data = load_limits(base_json)
    except (OSError, ValueError) as exc:
        return {}, {}, f"cannot read {base_json}: {exc}"
    checked, _skipped = evaluate(base_data, sections, base_root, use_env_overrides=False)
    from app.size_limit_policy import eligible_baseline_count

    breached: dict = {}
    for section, recs in checked.items():
        for rec in recs:
            base_count = eligible_baseline_count(
                rec.get("count"), rec.get("effective_cap", rec.get("cap"))
            )
            if base_count is not None:
                breached[(section, rec["rel"])] = base_count
    uncapped: dict = {}
    for section in sections:
        root = resolve_section_root(section, base_root, use_env_overrides=False)
        if root is None:
            continue
        suffixes, is_excluded = _SECTION_SCAN[section]
        caps = base_data.get(section) or {}
        for rec in scan_uncapped_section(
            root, caps, suffixes, is_excluded, section=section, repo_root=base_root
        ):
            uncapped[(section, rec["rel"])] = rec["count"]
    return breached, uncapped, None


def _split_preexisting(
    offenders: "list[tuple[str, dict]]",
    base_counts: dict,
    *,
    default_cap: int | None = None,
) -> "tuple[list, list]":
    """Partition offenders into (blocking, preexisting) against baseline counts.

    An offender is PRE-EXISTING — inherited from the base branch, not this
    change's responsibility — only when the same file was already an offender of
    the same class on base AND has not grown past its base line count here. A
    file that is clean on base, or that GREW while over-cap, is BLOCKING.

    Entries come back as ``(section, rec, base_count)`` — ``base_count`` is
    ``None`` for an offender absent from the baseline (including the
    no-baseline case, where ``base_counts`` is empty and everything blocks).
    """
    from app.size_limit_policy import is_breach

    blocking: list = []
    preexisting: list = []
    for section, rec in offenders:
        base_count = base_counts.get((section, rec["rel"]))
        cap = rec.get("effective_cap", rec.get("cap", default_cap))
        if is_breach(rec["count"], cap, base_count=base_count):
            blocking.append((section, rec, base_count))
        else:
            preexisting.append((section, rec, base_count))
    return blocking, preexisting


def _split_preexisting_records(
    offenders: list,
    base_identities: set[tuple] | None,
    identity,
) -> tuple[list, list]:
    """Partition non-size findings using exact identities from the base.

    ``None`` means the baseline could not establish this finding class. That
    is deliberately different from an empty set: unavailable evidence must
    fail closed, while an established clean baseline can excuse nothing.
    """
    if base_identities is None:
        return offenders, []
    blocking = [
        record for record in offenders if identity(record) not in base_identities
    ]
    preexisting = [
        record for record in offenders if identity(record) in base_identities
    ]
    return blocking, preexisting


def _ledger_record_identity(record: tuple) -> tuple:
    """Return the stable fields that identify one ledger finding."""
    return tuple(record[:5])


def _collect_baseline_governance_findings(
    base_root: str,
    sections: list[str],
    *,
    json_path: str,
    repo_root: str,
) -> tuple[set[tuple], set[tuple] | None, set[tuple] | None, str | None]:
    """Collect stale-cap and ledger findings from a base checkout.

    Size offenders have a line-count baseline, but stale caps and ledger
    findings are discrete records. They therefore need exact base identities.
    Ledger identities are unavailable when the base has no usable mainline git
    ref; returning ``None`` for those classes preserves fail-closed behavior.
    """
    relative_json = os.path.relpath(
        os.path.abspath(json_path), os.path.abspath(repo_root)
    )
    if relative_json == os.pardir or relative_json.startswith(
        os.pardir + os.sep
    ):
        return (
            set(),
            None,
            None,
            f"limits JSON {json_path} is outside repo root {repo_root}",
        )
    base_json = os.path.join(base_root, relative_json)
    try:
        base_data = load_limits(base_json)
    except (OSError, ValueError) as exc:
        return set(), None, None, f"cannot read {base_json}: {exc}"

    checked, _skipped = evaluate(
        base_data, sections, base_root, use_env_overrides=False
    )
    stale = {
        (section, rec["rel"])
        for section, records in checked.items()
        for rec in records
        if rec["count"] is None
    }

    from app.size_limit_policy import (
        LEDGER_BUMP_ABSENT,
        _mainline_git_ref,
        missing_ledger_bump_records,
        phantom_ledger_records,
    )

    # static_release_guard deliberately extracts the base tree with git
    # archive, so it has no .git entry. The candidate checkout is from the same
    # repository and supplies the mainline ancestry for the exact extracted
    # base files. A linked worktree is handled directly because its .git is a
    # file rather than a directory.
    git_root = base_root if _is_git_checkout(base_root) else repo_root
    if not _is_git_checkout(git_root):
        return stale, None, None, f"{base_root} has no usable git checkout"
    try:
        ref = _mainline_git_ref(git_root)
    except Exception as exc:
        return stale, None, None, f"could not resolve the base mainline ref: {exc}"
    if ref == "HEAD":
        return stale, None, None, "base checkout has no mainline branch"

    base_ledger = os.path.join(
        base_root,
        os.path.dirname(relative_json),
        "cap_bump_history.json",
    )
    phantoms = phantom_ledger_records(repo_root=git_root, ledger_path=base_ledger)
    missing = missing_ledger_bump_records(
        repo_root=git_root,
        caps_json_path=base_json,
        ledger_path=base_ledger,
    )
    return (
        stale,
        {_ledger_record_identity(record) for record in phantoms},
        {
            _ledger_record_identity(record)
            for record in missing
            if record[6] == LEDGER_BUMP_ABSENT
        },
        None,
    )


def _mainline_limits_via_git(repo_root: str) -> "tuple[dict | None, str | None]":
    """Read ``module_size_limits.json`` at the MAINLINE ancestry tip.

    Returns ``(limits, None)`` when the baseline is established, else
    ``(None, reason)`` — a clear skip, never a crash.  Reuses the
    mainline-ancestry scoping from ``app.size_limit_policy``
    (``_mainline_git_ref``): the ref whose ancestry is the cap-bump-history
    source of truth is also the baseline for "did this change RAISE a cap".
    ``git show <ref>:backend/testing/module_size_limits.json`` reads the caps at
    that ref directly — no checkout of the integration branch needed.

    Shallow-clone behavior: when the mainline ref is absent from the checkout
    (a depth-1 CI clone that never fetched the integration branch, or a
    task-VM shallow clone), or the file cannot be read at the ref, the caller
    must SKIP the cap-raise guard rather than fail — a missing ancestry
    baseline proves nothing either way. ``None`` with a reason is that skip.
    """
    from app.size_limit_policy import _mainline_git_ref

    if not _is_git_checkout(repo_root):
        return None, f"{repo_root} is not a git checkout"
    try:
        ref = _mainline_git_ref(repo_root)
    except Exception as exc:
        return None, f"could not resolve the mainline ref: {exc}"
    if ref == "HEAD":
        return None, "no mainline branch present (ref helper fell back to HEAD)"
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "show",
                f"{ref}:backend/testing/module_size_limits.json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=repo_root,
        )
    except Exception as exc:
        return None, f"git show {ref}:.../module_size_limits.json raised: {exc}"
    if result.returncode != 0:
        return None, (
            f"module_size_limits.json not readable at {ref} "
            f"(git show exit {result.returncode})"
        )
    try:
        limits = json.loads(result.stdout)
    except ValueError as exc:
        return None, f"module_size_limits.json at {ref} is not valid JSON: {exc}"
    if not isinstance(limits, dict):
        return None, (
            f"module_size_limits.json at {ref} is a JSON {type(limits).__name__}, "
            "not an object"
        )
    return limits, None


def _record_justifies(record, old_cap: int, new_cap: int) -> bool:
    """True when ``record`` is a cap-bump history entry for the exact raise.

    Accepts both the ledger's raw dict records and the ``BumpRecord`` dataclass
    ``parse_bump_history`` returns — the raise is justified only when a recorded
    bump matches the exact (old_cap -> new_cap) pair being guarded.
    """
    if isinstance(record, dict):
        return record.get("old_cap") == old_cap and record.get("new_cap") == new_cap
    return record.old_cap == old_cap and record.new_cap == new_cap


def detect_unjustified_cap_raises(
    data: dict,
    mainline_data: dict,
    sections: "list[str]",
    *,
    history_for,
) -> "list[tuple[str, str, int, int]]":
    """Return ``(section, rel, old_cap, new_cap)`` for every cap in ``data``
    raised ABOVE the same module's cap on ``mainline_data`` with no ledger entry
    justifying the raise.

    Pure / filesystem-free: the caller supplies the mainline baseline and the
    ledger lookup (``history_for(section, rel)`` returns that module's cap-bump
    records). A raise with a matching (old_cap -> new_cap) record is deliberate
    and recorded; a raise without one is a SILENT cap bump and must fail the
    check. Lowerings and unchanged caps never need a ledger entry — the ratchet
    only guards the raising direction. A module absent from the baseline (a NEW
    cap for a previously-uncapped file) is not a raise either.
    """
    raises: list[tuple[str, str, int, int]] = []
    for section in sections:
        current_caps = data.get(section) or {}
        baseline_caps = mainline_data.get(section) or {}
        for rel, new_cap in current_caps.items():
            if not isinstance(new_cap, int):
                continue
            old_cap = baseline_caps.get(rel)
            if not isinstance(old_cap, int):
                # No cap on the mainline baseline — adding a cap for a new file
                # (previously uncapped) is not a raise and needs no ledger.
                continue
            if new_cap <= old_cap:
                # Unchanged or lowered — always legal, never needs a ledger entry.
                continue
            justified = any(
                _record_justifies(record, old_cap, new_cap)
                for record in history_for(section, rel)
            )
            if not justified:
                raises.append((section, rel, old_cap, new_cap))
    return raises


def _cap_bump_history_for(section: str, rel: str) -> list:
    """Return the cap-bump history ledger records for ``(section, rel)``.

    Delegates to ``app.size_limit_policy.parse_bump_history`` — the same source
    the chronic-offender verdict and the --fix regenerator use — so a raise is
    justified by exactly the history that classifies bumps.  An UNREADABLE
    history returns [] — a raise then has no justification and is flagged.  That
    fails CLOSED, matching the chronic-offender classifier's contract: unreadable
    evidence is never "no bump on record" a raise could lean on.
    """
    from app.size_limit_policy import BumpHistoryUnavailableError, parse_bump_history

    try:
        return parse_bump_history(rel, section)
    except BumpHistoryUnavailableError:
        return []


def _parse_sections(raw: str) -> list[str]:
    if raw.strip().lower() == "all":
        return list(SECTIONS)
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    invalid = [s for s in requested if s not in SECTIONS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"unknown section(s): {', '.join(invalid)}; choose from {', '.join(SECTIONS)}"
        )
    # Preserve canonical order regardless of how the user ordered them.
    return [s for s in SECTIONS if s in requested]


_SIZE_LIMITS_LANE = "size-limits"


def _record_baseline_attribution(
    *,
    base_root: str,
    baseline_reason: str | None,
    governance_baseline_reason: str | None,
    blocking_breaches: list,
    preexisting_breaches: list,
    blocking_stale_caps: list,
    preexisting_stale_caps: list,
    blocking_uncapped: list,
    preexisting_uncapped: list,
    blocking_phantoms: list,
    preexisting_phantoms: list,
    blocking_absent: list,
    preexisting_absent: list,
) -> None:
    """Record the baseline size verdict in both existing receipt projections.

    This is advisory evidence only. Both receipt writers are deliberately
    fail-open, and this wrapper keeps an unavailable emitter or an unexpected
    writer error from changing the checker verdict.
    """
    suppressed: dict[str, str] = {}
    introduced: dict[str, str] = {}

    def add(target: dict[str, str], kind: str, section: str, rel: str) -> None:
        origin = "inherited" if target is suppressed else "introduced"
        target[f"{section}/{rel}::{kind}"] = f"{origin} {kind} finding"

    groups = (
        (suppressed, "size-cap", ((s, r["rel"]) for s, r, *_ in preexisting_breaches)),
        (introduced, "size-cap", ((s, r["rel"]) for s, r, *_ in blocking_breaches)),
        (suppressed, "stale-cap", ((s, r["rel"]) for s, r in preexisting_stale_caps)),
        (introduced, "stale-cap", ((s, r["rel"]) for s, r in blocking_stale_caps)),
        (suppressed, "uncapped", ((s, r["rel"]) for s, r, *_ in preexisting_uncapped)),
        (introduced, "uncapped", ((s, r["rel"]) for s, r, *_ in blocking_uncapped)),
        (suppressed, "ledger-phantom", ((r[0], r[1]) for r in preexisting_phantoms)),
        (introduced, "ledger-phantom", ((r[0], r[1]) for r in blocking_phantoms)),
        (suppressed, "ledger-missing", ((r[0], r[1]) for r in preexisting_absent)),
        (introduced, "ledger-missing", ((r[0], r[1]) for r in blocking_absent)),
    )
    for target, kind, findings in groups:
        for section, rel in findings:
            add(target, kind, section, rel)

    reasons = [r for r in (baseline_reason, governance_baseline_reason) if r]
    if reasons:
        state = "baseline_unavailable"
    elif introduced:
        state = "test_failure"
    elif suppressed:
        state = "preexisting_test_failure"
    else:
        state = "passed"
    extra = {"base_root": base_root}
    if reasons:
        # The receipt writer bounds failure identities/details, but not free-form
        # extras. Keep exception text bounded before handing it to that API.
        extra["baseline_reason"] = "; ".join(reasons)[:1000]

    try:
        from verification_receipt import record_lane as record_verification_lane

        record_verification_lane(
            _SIZE_LIMITS_LANE,
            state=state,
            blocking=bool(introduced),
            new_failures=introduced,
            suppressed_failures=suppressed,
            extra=extra,
        )
    except Exception as exc:  # receipt evidence is fail-open
        print(
            f"[verification-receipt] WARN could not record {_SIZE_LIMITS_LANE}: "
            f"{exc}{_SCRIPTS_DIR_HINT}",
            file=sys.stderr,
        )

    try:
        from guard_suppression_receipt import record_lane as record_suppression_lane

        record_suppression_lane(
            _SIZE_LIMITS_LANE,
            state=state,
            suppressed=suppressed,
            note=(f"baseline unavailable: {extra['baseline_reason']}" if reasons else None),
        )
    except Exception as exc:  # receipt evidence is fail-open
        print(
            f"[guard-suppression] WARN could not record {_SIZE_LIMITS_LANE}: "
            f"{exc}{_SCRIPTS_DIR_HINT}",
            file=sys.stderr,
        )


def _parse_repair_target(raw: str) -> str:
    """Validate a repeatable ``section/repo-relative-path`` repair selector.

    This selector is intentionally narrower than ``--sections``: a caller that
    has already attributed one task-caused offender must not turn that repair
    into a sweep over every breach presently visible in the checkout.
    """
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise argparse.ArgumentTypeError(
            "repair target must be a forward-slash section/path"
        )
    section, separator, rel = raw.partition("/")
    parts = rel.split("/") if separator else []
    if (
        section not in SECTIONS
        or not rel
        or any(part in {"", ".", ".."} for part in parts)
        or os.path.isabs(rel)
    ):
        raise argparse.ArgumentTypeError(
            "repair target must be a known section followed by a safe relative path"
        )
    return f"{section}/{rel}"


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        description="Check (and optionally --fix) module size-limit guardrails."
    )
    parser.add_argument(
        "--sections",
        type=_parse_sections,
        default=list(SECTIONS),
        help="comma-separated subset of {backend,agent,frontend} (default: all)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="bump the cap of each breached file to the canonical policy value",
    )
    parser.add_argument(
        "--repair-target",
        action="append",
        type=_parse_repair_target,
        default=[],
        metavar="SECTION/PATH",
        help=(
            "with --fix, repair only this current breached or uncapped file; "
            "repeatable and intended for baseline-attributed task repair"
        ),
    )
    parser.add_argument(
        "--force-chronic",
        action="store_true",
        dest="force_chronic",
        help=(
            "operator-approval override: allow --fix to bump a chronic offender's cap. "
            f"Requires {_OPERATOR_APPROVAL_ENV} in the environment -- it is an operator "
            "lever, not a task-agent one. Attribution is recorded in the output. Without "
            "this flag, chronic offenders are skipped and exit 1 is returned."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=_DEFAULT_JSON_PATH,
        help="path to module_size_limits.json (default: alongside this script)",
    )
    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        default=None,
        help="repo root used to resolve section source trees (default: inferred)",
    )
    parser.add_argument(
        "--base-root",
        dest="base_root",
        default=None,
        help=(
            "path to a checkout of the BASE branch (e.g. a detached worktree of "
            "origin/automain). Check mode then blocks only on offenders "
            "introduced or grown by this change; an offender already present at "
            "the same-or-larger size on base (judged against the base's own "
            "module_size_limits.json) is reported as [pre-existing] and does "
            "not affect the exit code. Ignored under --fix."
        ),
    )
    args = parser.parse_args(argv)
    if args.repair_target and not args.fix:
        parser.error("--repair-target requires --fix")
    if len(args.repair_target) != len(set(args.repair_target)):
        parser.error("--repair-target values must be unique")
    if args.force_chronic and not os.environ.get(_OPERATOR_APPROVAL_ENV, "").strip():
        print(
            f"[refused] --force-chronic requires operator approval: set "
            f"{_OPERATOR_APPROVAL_ENV} to the approving operator's identifier. A task "
            "agent must not pass this flag -- decompose the module, or raise a blocker "
            "so an operator can decide. See PR #16787 for a reverted unapproved bump.",
            file=sys.stderr,
        )
        return 1
    repo_root_explicit = args.repo_root is not None
    if args.repo_root is None:
        args.repo_root = _DEFAULT_REPO_ROOT

    data = load_limits(args.json_path)
    # The environment-specific frontend escape hatches (ADMIN_UI_SRC /
    # /admin-ui/src) apply only to the real run when the caller did not pass an
    # explicit --repo-root. An explicit root means hermetic resolution under
    # that checkout, even if it happens to equal the default path.
    use_env_overrides = not repo_root_explicit
    checked, skipped = evaluate(
        data, args.sections, args.repo_root, use_env_overrides=use_env_overrides
    )

    for section in skipped:
        print(f"[skip] {section}: source tree not present in this environment")

    breaches = [
        (section, rec)
        for section in args.sections
        for rec in checked.get(section, [])
        if rec["breach"]
    ]
    # A missing capped file is stale governance, not a harmless non-breach:
    # check mode reports it clearly and --fix removes the cap and its ledger
    # history.  Retain the scan record's count=None behavior for API consumers.
    stale_caps = [
        (section, rec)
        for section in args.sections
        for rec in checked.get(section, [])
        if rec["count"] is None
    ]

    # Complementary guardrail: uncapped source files over the coverage threshold.
    # A capped-but-breached file is handled by ``breaches`` above; a file with no
    # cap at all is invisible to that scan, so walk each scoped section's tree.
    uncapped: list[tuple[str, dict]] = []
    for section in args.sections:
        if section in skipped:
            continue
        # Keep the uncapped scan on the same root resolution path as evaluate().
        root = resolve_section_root(
            section, args.repo_root, use_env_overrides=use_env_overrides
        )
        if root is None:
            continue
        suffixes, is_excluded = _SECTION_SCAN[section]
        caps = data.get(section) or {}
        uncapped.extend(
            (section, rec)
            for rec in scan_uncapped_section(
                root,
                caps,
                suffixes,
                is_excluded,
                section=section,
                repo_root=args.repo_root,
            )
        )

    from app.size_limit_policy import (
        LEDGER_BUMP_ABSENT,
        missing_ledger_bump_records,
        phantom_ledger_records,
    )

    ledger_path = os.path.join(
        os.path.dirname(args.json_path), "cap_bump_history.json"
    )
    missing_ledger_records = missing_ledger_bump_records(
        repo_root=args.repo_root,
        caps_json_path=args.json_path,
        ledger_path=ledger_path,
    )
    # Split by severity. An ABSENT record is real ledger drift: the deployed
    # chronic-offender classifier is missing a bump it should count, so it fails
    # the check. An UNATTRIBUTED one only lacks provenance — the bump itself IS
    # recorded — and is the unavoidable residue of every rollup auto-bump, which
    # must stage its record before the commit SHA exists. Failing on it would
    # turn ./check red on every branch until an operator ran --fix by hand.
    # Both are repaired by the same --fix rebuild below.
    absent_ledger_records = [
        rec for rec in missing_ledger_records if rec[6] == LEDGER_BUMP_ABSENT
    ]
    unattributed_ledger_records = [
        rec for rec in missing_ledger_records if rec[6] != LEDGER_BUMP_ABSENT
    ]

    if args.fix:
        scoped_repair = bool(args.repair_target)
        if scoped_repair:
            requested_targets = set(args.repair_target)
            actionable_targets = {
                f"{section}/{rec['rel']}" for section, rec in breaches + uncapped
            }
            invalid_targets = sorted(requested_targets - actionable_targets)
            if invalid_targets:
                print(
                    "[refused] repair target(s) are not current cap breaches or "
                    "uncapped oversized files: " + ", ".join(invalid_targets)
                )
                return 1
            # A targeted task repair must not also prune stale caps or heal
            # unrelated ledger drift. Those unscoped maintenance actions retain
            # their historic --fix behavior, but are unsafe when this command is
            # responding to one attributed failure occurrence.
            breaches = [
                (section, rec)
                for section, rec in breaches
                if f"{section}/{rec['rel']}" in requested_targets
            ]
            uncapped = [
                (section, rec)
                for section, rec in uncapped
                if f"{section}/{rec['rel']}" in requested_targets
            ]
            stale_caps = []
            absent_ledger_records = []
            unattributed_ledger_records = []
            missing_ledger_records = []

        # Ledger-phantom self-heal: even with no cap breach to bump, a
        # cap-bump-history ledger holding records the mainline git history
        # cannot justify must be pruned so the check (and the deployed
        # chronic-offender classification) stops trusting phantom records.
        phantoms = phantom_ledger_records(
            repo_root=args.repo_root, ledger_path=ledger_path
        )
        if scoped_repair:
            phantoms = []
        if (
            not breaches
            and not uncapped
            and not stale_caps
            and not phantoms
            and not missing_ledger_records
        ):
            print("No breaches to fix — all scoped files within cap.")
            return 0
        chronic_skipped: list[tuple[str, dict, str]] = []
        bumped: list[tuple[str, dict, int]] = []
        added: list[tuple[str, dict, int]] = []
        pruned: list[tuple[str, dict]] = []
        for section, rec in breaches:
            verdict = classify_size_record(
                rec["rel"],
                rec["count"],
                rec["cap"],
                section,
                override_chronic=args.force_chronic,
                repo_root=args.repo_root,
            )["verdict"]
            if verdict["suggested_new_limit"] is None:
                # First-class refusal — chronic (no override) or already beyond
                # governance (no override can help; never suggest one).
                governance_refusal = verdict["reason"].startswith("Refusing cap bump:")
                hint = "" if governance_refusal else " Skipping cap bump: a chronic offender is not task-bumpable. Decompose the module, or raise a blocker for an operator."
                label = "[refused]" if governance_refusal else "[chronic]"
                print(f"{label} {section}/{rec['rel']}: {rec['count']} lines — {verdict['reason']}{hint}")
                chronic_skipped.append((section, rec, verdict["reason"]))
            else:
                new_cap = verdict["suggested_new_limit"]
                data[section][rec["rel"]] = new_cap
                label = "[fix-chronic]" if verdict["is_chronic"] else "[fix]"
                print(
                    f"{label}  {section}/{rec['rel']}: {rec['count']} lines — "
                    f"cap {rec['cap']} -> {new_cap}"
                    + (
                        f" (operator-approved; {verdict['bump_count']} prior bumps)"
                        if verdict["is_chronic"]
                        else ""
                    )
                )
                bumped.append((section, rec, new_cap))
        for section, rec in uncapped:
            # Uncapped files have no bump history to check — they are new to the
            # tracking system, so they cannot be chronic offenders yet.
            new_cap = recommended_cap_for_line_count(
                rec["count"], rec["rel"], section, args.repo_root
            )
            data.setdefault(section, {})[rec["rel"]] = new_cap
            print(
                f"[add]  {section}/{rec['rel']}: {rec['count']} lines — "
                f"added cap {new_cap}"
            )
            added.append((section, rec, new_cap))
        for section, rec in stale_caps:
            data.get(section, {}).pop(rec["rel"], None)
            print(f"[prune] {section}/{rec['rel']}: removed stale cap record")
            pruned.append((section, rec))

        # Validate every source rewrite before changing any tracked artifact.
        # A malformed governing declaration must fail closed instead of leaving
        # a JSON cap and source constant out of sync.
        source_rewrites: list[tuple[str, str]] = []
        if bumped:
            from app.size_limit_policy import (
                governing_cap_source_path,
                rewrite_governing_cap_constant,
            )

            for section, rec, new_cap in bumped:
                source_rel = governing_cap_source_path(section, rec["rel"])
                if source_rel is None:
                    continue
                source_path = os.path.join(args.repo_root, source_rel)
                try:
                    with open(source_path, encoding="utf-8") as source_file:
                        source_text = source_file.read()
                    source_rewrites.append(
                        (
                            source_path,
                            rewrite_governing_cap_constant(
                                section, rec["rel"], source_text, new_cap
                            ),
                        )
                    )
                except (OSError, ValueError) as exc:
                    print(
                        f"[governing-cap] cannot repair {section}/{rec['rel']}: {exc}"
                    )
                    return 1

        repair_paths = [args.json_path, ledger_path] + [
            source_path for source_path, _source_text in source_rewrites
        ]
        snapshots = _snapshot_artifacts(repair_paths)
        try:
            if bumped or added or pruned:
                write_limits(args.json_path, data)
            for source_path, source_text in source_rewrites:
                # newline="": source_text was read with universal newlines, so
                # without it a Windows --fix rewrites the WHOLE tracked source
                # file as CRLF to change one cap constant.
                with open(source_path, "w", encoding="utf-8", newline="") as source_file:
                    source_file.write(source_text)
            if bumped or pruned or phantoms or missing_ledger_records:
                # Keep the committed cap-bump-history ledger (the deployed
                # backend's history source) in sync with the bumps just applied
                # AND self-heal it. All cap/source/ledger writes are one repair
                # transaction from the checker's perspective: a ledger failure
                # restores every artifact so a successful-looking partial
                # repair can never leave governance incoherent.
                from app.size_limit_policy import rebuild_cap_bump_history_ledger

                rebuild_cap_bump_history_ledger(
                    caps_json_path=args.json_path,
                    ledger_path=ledger_path,
                    pending=[
                        (section, rec["rel"], rec["cap"], new_cap)
                        for section, rec, new_cap in bumped
                    ]
                    # --fix repairs BOTH severities: an absent record is
                    # appended, an unattributed one is upgraded in place with
                    # the observed provenance. The trailing status field is
                    # advisory for reporting only, so drop it here.
                    + [rec[:6] for rec in missing_ledger_records],
                    repair_targets=(
                        {(section, rec["rel"]) for section, rec, _new_cap in bumped}
                        if scoped_repair
                        else None
                    ),
                )
                if phantoms:
                    print(
                        f"Pruned {len(phantoms)} phantom record(s) from "
                        f"{os.path.basename(ledger_path)}."
                    )
                if absent_ledger_records:
                    print(
                        f"Restored {len(absent_ledger_records)} git-recorded "
                        f"bump(s) in {os.path.basename(ledger_path)}."
                    )
                if unattributed_ledger_records:
                    print(
                        f"Attributed {len(unattributed_ledger_records)} pending "
                        f"cap-bump record(s) in "
                        f"{os.path.basename(ledger_path)}."
                    )
                if bumped:
                    print(f"Updated {os.path.basename(ledger_path)}.")
        except Exception as exc:  # repair must fail closed
            try:
                _restore_artifacts(snapshots)
            except Exception as restore_exc:
                print(
                    f"[error] cap repair failed and rollback failed: {exc}; "
                    f"rollback error: {restore_exc}"
                )
            else:
                print(f"[error] cap repair rolled back: {exc}")
            return 1
        actions = []
        if bumped:
            actions.append(f"Bumped {len(bumped)} cap(s)")
        if added:
            actions.append(f"Added {len(added)} cap(s)")
        if pruned:
            actions.append(f"Pruned {len(pruned)} stale cap(s)")
        if absent_ledger_records:
            actions.append(f"Restored {len(absent_ledger_records)} ledger bump(s)")
        if unattributed_ledger_records:
            actions.append(
                f"Attributed {len(unattributed_ledger_records)} ledger bump(s)"
            )
        if actions:
            print(f"{'; '.join(actions)} in {args.json_path}.")
        if chronic_skipped:
            print(
                f"\n{len(chronic_skipped)} cap bump(s) refused (see reasons above) — decompose "
                "the module, or for a governance-tightened cap make a deliberate paired raise "
                f"of the source-declared *_SIZE_GOVERNANCE_CAP constant AND its {os.path.basename(args.json_path)} entry."
            )
            return 1
        return 0

    # Baseline comparison: with --base-root, only offenders INTRODUCED or GROWN
    # by this change block; offenders inherited from the base branch at the same
    # or larger size there are reported as [pre-existing] and never fail the
    # check — an upstream breach (e.g. a merge that pushed a chronic offender
    # over its cap on automain) must not block every unrelated PR. An unreadable
    # baseline fails CLOSED: everything is treated as newly introduced.
    base_breached: dict = {}
    base_uncapped: dict = {}
    base_ungoverned_release_modules: set[str] | None = set()
    if args.base_root:
        base_breached, base_uncapped, baseline_reason = _collect_baseline_offenders(
            args.base_root, args.sections
        )
        if baseline_reason:
            print(
                f"[baseline] unavailable ({baseline_reason}) — treating every "
                "offender as introduced by this change"
            )

        try:
            base_ungoverned_release_modules = set(
                find_ungoverned_release_surface_modules(args.base_root)
            )
        except (OSError, ValueError) as exc:
            base_ungoverned_release_modules = None
            print(
                f"[baseline] ungoverned release findings unavailable "
                f"({exc}) — treating every finding as introduced by this change"
            )

    base_stale_caps: set[tuple] = set()
    base_phantom_identities: set[tuple] | None = set()
    base_absent_identities: set[tuple] | None = set()
    if args.base_root:
        (
            base_stale_caps,
            base_phantom_identities,
            base_absent_identities,
            governance_baseline_reason,
        ) = _collect_baseline_governance_findings(
            args.base_root,
            args.sections,
            json_path=args.json_path,
            repo_root=args.repo_root,
        )
        if governance_baseline_reason:
            print(
                f"[baseline] governance findings unavailable "
                f"({governance_baseline_reason}) — treating every stale or "
                "ledger finding as introduced by this change"
            )

    exit_code = 0
    blocking_breaches, preexisting_breaches = _split_preexisting(
        breaches, base_breached
    )
    if blocking_breaches:
        from app.size_limit_policy import (
            governance_source_relpath,
            is_breach,
            resolve_effective_cap,
        )

        # A module already beyond its OWN governance cap cannot be repaired by
        # --fix/--force-chronic (both refuse unconditionally) — cheap AST-only
        # check, no git, so guidance never points at a dead end.
        zero_headroom: list[tuple[str, dict]] = []
        for section, rec, base_count in blocking_breaches:
            grew = f"; grew from {base_count} lines on base" if base_count is not None else ""
            cap_note = (
                f"cap: {rec['effective_cap']}"
                if rec.get("effective_cap") == rec.get("cap")
                else f"cap: {rec['effective_cap']} (governance-tightened from JSON {rec['cap']})"
                if rec.get("cap") is not None
                else f"cap: {rec['effective_cap']} (governance cap, no JSON entry)"
            )
            print(f"[breach] {section}/{rec['rel']}: {rec['count']} lines ({cap_note}){grew}")
            governance_cap = resolve_effective_cap(rec["rel"], section, None, repo_root=args.repo_root)
            if governance_cap is not None and is_breach(rec["count"], governance_cap):
                zero_headroom.append((section, rec))

        fixable_count = len(blocking_breaches) - len(zero_headroom)
        if fixable_count:
            print(f"\n{fixable_count} size breach(es) fixable. Run with --fix to bump the breached caps to the canonical policy value.")
        if zero_headroom:
            print(
                f"\n{len(zero_headroom)} size breach(es) are already beyond their own governance "
                "cap — --fix and --force-chronic cannot bump these. Decompose the module, or make "
                f"a deliberate paired raise of the source-declared *_SIZE_GOVERNANCE_CAP constant AND its {args.json_path} entry:"
            )
            for section, rec in zero_headroom:
                source_path = governance_source_relpath(section, rec["rel"]) or "<source module>"
                print(f"  {section}/{rec['rel']}: raise the cap in {source_path} and {args.json_path}")
        exit_code = 1

    blocking_stale_caps, preexisting_stale_caps = _split_preexisting_records(
        stale_caps,
        base_stale_caps,
        lambda record: (record[0], record[1]["rel"]),
    )
    if blocking_stale_caps:
        for section, rec in blocking_stale_caps:
            print(
                f"[stale-cap] {section}/{rec['rel']}: cap {rec['cap']} targets "
                "a missing file — run with --fix to remove the stale cap and "
                "its ledger history"
            )
        exit_code = 1

    blocking_uncapped, preexisting_uncapped = _split_preexisting(
        uncapped, base_uncapped, default_cap=_COVERAGE_THRESHOLD
    )
    if blocking_uncapped:
        for section, rec, base_count in blocking_uncapped:
            grew = f"; grew from {base_count} lines on base" if base_count is not None else ""
            print(
                f"[uncapped] {section}/{rec['rel']}: {rec['count']} lines "
                f"(threshold: {_COVERAGE_THRESHOLD}) — NO cap entry{grew}"
            )
        print(
            f"\n{len(blocking_uncapped)} uncapped file(s) over "
            f"{_COVERAGE_THRESHOLD} lines violate the size guardrail (no cap "
            f"entry). Run with --fix to add a cap entry (current size + policy "
            f"headroom)."
        )
        exit_code = 1

    if "backend" not in skipped:
        ungoverned_release_modules = find_ungoverned_release_surface_modules(
            args.repo_root
        )
        inherited_ungoverned_release_modules = []
        if base_ungoverned_release_modules is not None:
            inherited_ungoverned_release_modules = [
                name
                for name in ungoverned_release_modules
                if name in base_ungoverned_release_modules
            ]
            ungoverned_release_modules = [
                name
                for name in ungoverned_release_modules
                if name not in base_ungoverned_release_modules
            ]
        if ungoverned_release_modules:
            for name in ungoverned_release_modules:
                print(
                    f"[ungoverned] backend/{name}: on the release/verification "
                    "surface with no declared _MODULE_SIZE_GOVERNANCE_CAP — add "
                    "one (see backend/app/release_qualifier.py for the pattern) "
                    "before this module can grow further"
                )
            print(
                f"\n{len(ungoverned_release_modules)} release-surface module(s) "
                "missing a governance cap declaration."
            )
            exit_code = 1
        if base_ungoverned_release_modules is not None:
            for name in inherited_ungoverned_release_modules:
                print(
                    f"[pre-existing] backend/{name}: ungoverned release-surface "
                    "module inherited from the base branch"
                )
    else:
        inherited_ungoverned_release_modules = []

    preexisting = [
        ("cap breach", section, rec, base_count)
        for section, rec, base_count in preexisting_breaches
    ] + [
        ("uncapped", section, rec, base_count)
        for section, rec, base_count in preexisting_uncapped
    ]
    preexisting_count = len(preexisting) + len(inherited_ungoverned_release_modules)
    for section, rec in preexisting_stale_caps:
        print(
            f"[pre-existing] {section}/{rec['rel']}: stale cap targets a missing "
            "file on the base branch — inherited from the base branch, not "
            "introduced by this change"
        )
    preexisting_count += len(preexisting_stale_caps)
    if preexisting:
        for kind, section, rec, base_count in preexisting:
            print(
                f"[pre-existing] {section}/{rec['rel']}: {rec['count']} lines "
                f"({kind}; already {base_count} lines on base) — inherited from "
                f"the base branch, not introduced by this change"
            )
        print(
            f"\n{len(preexisting)} pre-existing offender(s) inherited from the "
            f"base branch — NOT blocking this change. Upstream remediation "
            f"needed: a dedicated chore PR running check_size_limits.py --fix "
            f"(chronic offenders require operator approval via --force-chronic)."
        )

    # Cap-raise guardrail: the ratchet must only ever be WEAKENED deliberately.
    # A check that merely asks "is each file under its cap" passes MORE easily
    # when a cap is raised, so compare this change's caps against the MAINLINE
    # ancestry (not an arbitrary base) and fail when a raise has no
    # cap_bump_history.json entry justifying it. A matching ledger entry proves
    # the raise was recorded (via --fix or an explicit ledger edit); lowering a
    # cap is always legal; a raise without a ledger entry is a silent cap bump
    # and blocks the check. When the mainline baseline cannot be established
    # (not a git checkout, no mainline branch, unreadable file at the ref) the
    # guard degrades to a clear skip — a missing ancestry baseline proves
    # nothing either way.
    mainline_data, mainline_reason = _mainline_limits_via_git(args.repo_root)
    if mainline_data is None:
        print(f"[cap-raise-guard] skipped: {mainline_reason}")
    else:
        unjustified_raises = detect_unjustified_cap_raises(
            data,
            mainline_data,
            args.sections,
            history_for=_cap_bump_history_for,
        )
        if unjustified_raises:
            for section, rel, old_cap, new_cap in unjustified_raises:
                print(
                    f"[cap-raise] {section}/{rel}: cap raised {old_cap} -> {new_cap} "
                    "with no cap-bump-history entry justifying the raise"
                )
            print(
                f"\n{len(unjustified_raises)} unjustified cap raise(s). Raising a "
                "cap must be deliberate AND recorded: run this checker with --fix "
                "(which bumps the cap and appends the bump to cap_bump_history.json), "
                "or add a cap_bump_history.json entry for the "
                f"({old_cap} -> {new_cap}) raise."
            )
            exit_code = 1

    # Ledger-phantom guard: the cap-bump-history ledger must not hold records
    # the MAINLINE git history cannot justify — the reverse direction of the
    # [cap-raise] guard above. Where [cap-raise] proves git is recorded in the
    # ledger, this proves the ledger is recorded in git: every ledger record
    # must correspond to a real increase actually made by the named commit on
    # the mainline ancestry. A record with a wrong old_cap, a decrease mis-filed
    # as a bump, a commit outside the mainline ancestry, or an unresolvable SHA
    # is a phantom. This check runs on truncated/shallow checkouts too — every
    # CI shard and agent VM — so phantom drift is caught where the legacy
    # truncated-checkout skip let it hide. When git history is unavailable (not
    # a git checkout, no mainline ref) the guard degrades to a clear skip, never
    # a crash or a spurious failure.
    phantoms = phantom_ledger_records(
        repo_root=args.repo_root, ledger_path=ledger_path
    )
    blocking_phantoms, preexisting_phantoms = _split_preexisting_records(
        phantoms, base_phantom_identities, _ledger_record_identity
    )
    if blocking_phantoms:
        for section, key, old_cap, new_cap, commit_sha, reason in blocking_phantoms:
            print(
                f"[ledger-phantom] {section}/{key}: ledger records "
                f"{old_cap} -> {new_cap} at {commit_sha[:12]} — {reason}"
            )
        print(
            f"\n{len(blocking_phantoms)} phantom cap-bump-history record(s) — records the "
            "mainline git history cannot justify. Run this checker with --fix to "
            f"rebuild {os.path.basename(ledger_path)} from git (pruning phantoms), "
            "or remove the listed records from the ledger."
        )
        exit_code = 1
    for section, key, old_cap, new_cap, commit_sha, _reason in preexisting_phantoms:
        print(
            f"[pre-existing] {section}/{key}: ledger phantom {old_cap} -> "
            f"{new_cap} at {commit_sha[:12]} — inherited from the base branch, "
            "not introduced by this change"
        )
    preexisting_count += len(preexisting_phantoms)

    # Pending attribution is NOT drift: the ledger holds the bump, only its
    # provenance is still the placeholder every pre-commit auto-bump must write.
    # Report it as a reconcile hint so it stays visible and repairable, but do
    # NOT fail — a rollup auto-bump landing on mainline would otherwise break
    # ./check for every agent on every branch until an operator ran --fix.
    if unattributed_ledger_records:
        for (
            section,
            rel,
            old_cap,
            new_cap,
            commit_sha,
            _commit_date,
            _status,
        ) in unattributed_ledger_records:
            print(
                f"[ledger-pending-attribution] {section}/{rel}: cap-bump-history "
                f"records {old_cap} -> {new_cap} but still carries the pre-commit "
                f"placeholder; git attributes it to {commit_sha[:12]}"
            )
        print(
            f"\n{len(unattributed_ledger_records)} ledger cap bump(s) await commit "
            "attribution (not a failure). Run this checker with --fix to stamp "
            f"the observed provenance into {os.path.basename(ledger_path)}."
        )

    blocking_absent, preexisting_absent = _split_preexisting_records(
        absent_ledger_records, base_absent_identities, _ledger_record_identity
    )
    if blocking_absent:
        for (
            section,
            rel,
            old_cap,
            new_cap,
            commit_sha,
            _commit_date,
            _status,
        ) in blocking_absent:
            print(
                f"[ledger-missing] {section}/{rel}: git records {old_cap} -> "
                f"{new_cap} at {commit_sha[:12]}, but cap-bump-history has no "
                "matching evidence"
            )
        print(
            f"\n{len(blocking_absent)} git-recorded cap bump(s) are "
            "missing from the ledger. Run this checker with --fix to rebuild "
            f"{os.path.basename(ledger_path)}."
        )
        exit_code = 1
    for section, rel, old_cap, new_cap, commit_sha, _date, _status in preexisting_absent:
        print(
            f"[pre-existing] {section}/{rel}: ledger missing {old_cap} -> "
            f"{new_cap} at {commit_sha[:12]} — inherited from the base branch, "
            "not introduced by this change"
        )
    preexisting_count += len(preexisting_absent)

    if args.base_root:
        _record_baseline_attribution(
            base_root=args.base_root,
            baseline_reason=baseline_reason,
            governance_baseline_reason=governance_baseline_reason,
            blocking_breaches=blocking_breaches,
            preexisting_breaches=preexisting_breaches,
            blocking_stale_caps=blocking_stale_caps,
            preexisting_stale_caps=preexisting_stale_caps,
            blocking_uncapped=blocking_uncapped,
            preexisting_uncapped=preexisting_uncapped,
            blocking_phantoms=blocking_phantoms,
            preexisting_phantoms=preexisting_phantoms,
            blocking_absent=blocking_absent,
            preexisting_absent=preexisting_absent,
        )

    if exit_code == 0 and not preexisting and preexisting_count == 0:
        print("All scoped files within their size caps.")
    elif preexisting_count:
        print(
            f"\n{preexisting_count} pre-existing governance finding(s) inherited "
            "from the base branch — NOT blocking this change."
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
