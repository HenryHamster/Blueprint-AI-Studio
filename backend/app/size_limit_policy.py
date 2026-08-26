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

import ast
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple, TypedDict, TypeVar


class GovernanceCapExceededError(ValueError):
    """Raised when a file is already beyond its declared governing cap."""


_SECTION_SOURCE_DIRS = {
    "backend": ("backend", "app"),
    "agent": ("agent", "src"),
    "frontend": ("admin-ui", "src"),
    "testing": ("backend", "testing"),
    "scripts": ("scripts",),
}

# The authoritative section-key set: the top-level keys of
# ``module_size_limits.json``. Every consumer that scans, maps, or gates on a
# section (``ci_compliance``, ``rollup_size_preflight``, ``hotfix_backmerge``,
# ``check_size_limits``) must cover exactly this set — a consumer with a
# hand-copied subset silently under-reports the missing sections. Kept as a
# literal (not merely ``frozenset(_SECTION_SOURCE_DIRS)``) so a future 6th
# section added to one but not the other fails a parity assertion loudly
# instead of both sides silently agreeing on the smaller set.
SECTION_KEYS: frozenset[str] = frozenset(
    {"backend", "agent", "frontend", "testing", "scripts"}
)
assert frozenset(_SECTION_SOURCE_DIRS) == SECTION_KEYS, (
    "_SECTION_SOURCE_DIRS must declare exactly SECTION_KEYS"
)


def _no_section_exclusion(_rel_path: str) -> bool:
    return False


def _is_frontend_section_excluded(rel_path: str) -> bool:
    """Mirror check_size_limits._is_frontend_excluded: skip test/type files."""
    return rel_path.endswith((".test.ts", ".test.tsx", ".d.ts")) or rel_path.startswith(
        "test/"
    )


def _is_agent_section_excluded(rel_path: str) -> bool:
    """Mirror check_size_limits._is_agent_excluded: skip test/type files."""
    return rel_path.endswith((".test.ts", ".d.ts"))


# Repo-relative path prefix each section's ``rel`` keys are rooted at, derived
# from ``_SECTION_SOURCE_DIRS`` so the two can never drift apart. Consumers
# that need a trailing-slash string prefix (``ci_compliance``,
# ``rollup_size_preflight``) use this directly instead of hand-copying a
# second literal table.
SECTION_PATH_PREFIXES: dict[str, str] = {
    section: "/".join(parts) + "/" for section, parts in _SECTION_SOURCE_DIRS.items()
}

# Ordered longest-prefix-first for consumers (``hotfix_backmerge``) that match
# a changed file's path against each section prefix with ``startswith`` — a
# more-specific prefix must always be tried before a shorter one that could
# otherwise shadow it. No two section prefixes here are actually in a
# prefix relationship (verified by a guard test), but the ordering keeps that
# true by construction rather than by accident of dict insertion order.
SECTION_PREFIX_ORDER: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((prefix, section) for section, prefix in SECTION_PATH_PREFIXES.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

# Per-section source file extensions scanned for the uncapped-coverage
# guardrail. Mirrors check_size_limits._SECTION_SCAN.
SECTION_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "backend": (".py",),
    "agent": (".ts",),
    "frontend": (".ts", ".tsx"),
    "testing": (".py",),
    "scripts": (".py",),
}

# Per-section exclusion predicate for the uncapped-coverage guardrail. Mirrors
# check_size_limits._SECTION_SCAN (backend/testing/scripts scan every file of
# their extension; agent/frontend skip their own test/type files).
SECTION_EXCLUDED: dict[str, Callable[[str], bool]] = {
    "backend": _no_section_exclusion,
    "agent": _is_agent_section_excluded,
    "frontend": _is_frontend_section_excluded,
    "testing": _no_section_exclusion,
    "scripts": _no_section_exclusion,
}


def governance_caps_from_source_text(source_text: str) -> list[int]:
    """``*_SIZE_GOVERNANCE_CAP`` int declarations in source text (no disk I/O)."""
    # Exact, not heuristic: the matcher below accepts only a NAME ending in this
    # same literal. Skipping ast.parse here: five-section scan 21.25s -> 1.86s.
    if "_SIZE_GOVERNANCE_CAP" not in source_text:
        return []
    try:
        tree = ast.parse(source_text)
    except (SyntaxError, ValueError):
        return []

    caps: list[int] = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
            value = node.value
        else:
            continue
        if not isinstance(value, ast.Constant) or isinstance(value.value, bool):
            continue
        if not isinstance(value.value, int):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith(
                "_SIZE_GOVERNANCE_CAP"
            ):
                caps.append(value.value)
    return caps


def _governing_caps_for_module(
    module_key: str, section: str, repo_root: str | None = None
) -> list[int]:
    """Discover integer ``*_SIZE_GOVERNANCE_CAP`` declarations for a module.

    Governance belongs next to the source it protects. Reading the declaration
    from the module's AST keeps this policy generic: a future module can opt in
    by declaring its own governing constant without adding a filename-specific
    branch here. Invalid or unavailable source is treated as having no
    discovered relationship; the ordinary cap policy remains available.
    Reads the LOCAL tree; a caller holding ref-fetched text should call
    :func:`governance_caps_from_source_text` directly instead.
    """
    root = repo_root or _REPO_ROOT
    source_root_parts = _SECTION_SOURCE_DIRS.get(section)
    if source_root_parts is None:
        return []
    source_root = os.path.abspath(os.path.join(root, *source_root_parts))
    if section == "backend" and not os.path.isdir(source_root):
        # The production backend image installs the package at /app/app rather
        # than carrying a full repository checkout. Keep source-adjacent
        # governance effective there instead of silently applying generic
        # headroom.
        source_root = os.path.abspath(os.path.dirname(__file__))
    source_path = os.path.abspath(os.path.join(source_root, module_key))
    try:
        if os.path.commonpath((source_root, source_path)) != source_root:
            return []
        with open(source_path, encoding="utf-8") as source_file:
            source_text = source_file.read()
    except (OSError, UnicodeError, ValueError):
        return []
    return governance_caps_from_source_text(source_text)


def bump_size_limit(
    current_size: int,
    module_key: str | None = None,
    section: str = "backend",
    repo_root: str | None = None,
) -> int:
    """Return the new cap for a file that breached its size limit.

    Policy: the larger of ``current_size + 200`` (a flat floor so a small
    overrun still gets reasonable headroom) and ``int(current_size * 1.15)``
    (15% proportional growth so a large file isn't left with tiny headroom),
    rounded UP to the nearest 50 lines for stability.
    """
    floor = current_size + 200
    proportional = int(current_size * 1.15)
    raw = max(floor, proportional)
    proposed = ((raw + 49) // 50) * 50
    if module_key is None:
        return proposed

    governing_caps = _governing_caps_for_module(module_key, section, repo_root)
    if not governing_caps:
        return proposed
    governing_cap = min(governing_caps)
    if current_size > governing_cap:
        raise GovernanceCapExceededError(
            f"{section}/{module_key} is already {current_size} lines, beyond "
            f"its governing cap {governing_cap}"
        )
    return min(proposed, governing_cap)


class GoverningCap(TypedDict):
    """A source-declared cap that must move with its JSON counterpart."""

    source_path: str
    constant_name: str


class GoverningCapRepair(TypedDict):
    """The complete paired-source edit required for a governed cap repair."""

    source_path: str
    constant_name: str
    new_limit: int


# Most modules use the generic headroom policy above.  The release and
# acceptance surface is deliberately governed more tightly in each source
# module as well as in the JSON guardrail.  Keep this mapping explicit: both
# repair paths consume it, which prevents a future caller from silently
# applying the generic 15% rule to a module whose governance contract says
# otherwise.
_GOVERNING_CAPS: dict[tuple[str, str], GoverningCap] = {
    ("backend", "acceptance_criteria.py"): GoverningCap(
        source_path="backend/app/acceptance_criteria.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "acceptance_gate.py"): GoverningCap(
        source_path="backend/app/acceptance_gate.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "acceptance_provenance.py"): GoverningCap(
        source_path="backend/app/acceptance_provenance.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "acceptance_window_invariants.py"): GoverningCap(
        source_path="backend/app/acceptance_window_invariants.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "approved_pr_stranding.py"): GoverningCap(
        source_path="backend/app/approved_pr_stranding.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_activation.py"): GoverningCap(
        source_path="backend/app/release_activation.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_artifacts.py"): GoverningCap(
        source_path="backend/app/release_artifacts.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_commit_authorship.py"): GoverningCap(
        source_path="backend/app/release_commit_authorship.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_hotfix_promotion.py"): GoverningCap(
        source_path="backend/app/release_hotfix_promotion.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_models.py"): GoverningCap(
        source_path="backend/app/release_models.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_policy_projection.py"): GoverningCap(
        source_path="backend/app/release_policy_projection.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_prepromotion_rehearsal.py"): GoverningCap(
        source_path="backend/app/release_prepromotion_rehearsal.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_promotion.py"): GoverningCap(
        source_path="backend/app/release_promotion.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_promotion_pending.py"): GoverningCap(
        source_path="backend/app/release_promotion_pending.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_qualification_deferral.py"): GoverningCap(
        source_path="backend/app/release_qualification_deferral.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_qualifier.py"): GoverningCap(
        source_path="backend/app/release_qualifier.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_qualifier_audit.py"): GoverningCap(
        source_path="backend/app/release_qualifier_audit.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_qualifier_audit_surface.py"): GoverningCap(
        source_path="backend/app/release_qualifier_audit_surface.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_qualifier_hotfix_audit.py"): GoverningCap(
        source_path="backend/app/release_qualifier_hotfix_audit.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_qualifier_redrive.py"): GoverningCap(
        source_path="backend/app/release_qualifier_redrive.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_qualifier_sweep.py"): GoverningCap(
        source_path="backend/app/release_qualifier_sweep.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_readiness.py"): GoverningCap(
        source_path="backend/app/release_readiness.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_reconciliation.py"): GoverningCap(
        source_path="backend/app/release_reconciliation.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_scoring.py"): GoverningCap(
        source_path="backend/app/release_scoring.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_serving_confirmation.py"): GoverningCap(
        source_path="backend/app/release_serving_confirmation.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_stall_alerts.py"): GoverningCap(
        source_path="backend/app/release_stall_alerts.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_task_admission.py"): GoverningCap(
        source_path="backend/app/release_task_admission.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
    ("backend", "release_version.py"): GoverningCap(
        source_path="backend/app/release_version.py",
        constant_name="_MODULE_SIZE_GOVERNANCE_CAP",
    ),
}


def governing_cap_source_path(section: str, module_key: str) -> str | None:
    """Return the source path whose cap constant must move, if any."""
    governing = _GOVERNING_CAPS.get((section, module_key))
    return governing["source_path"] if governing else None


def governance_source_relpath(section: str, module_key: str) -> str | None:
    """Repo-relative path where ``module_key``'s own governance cap lives.

    Pure path construction; covers any governed module, unlike
    :func:`governing_cap_source_path` (curated ``_GOVERNING_CAPS`` only).
    """
    source_root_parts = _SECTION_SOURCE_DIRS.get(section)
    if source_root_parts is None:
        return None
    return "/".join((*source_root_parts, module_key))


def describe_effective_cap_breach(
    section: str,
    module_key: str,
    count: int,
    json_cap: int | None,
    effective_cap: int | None,
) -> str:
    """Name the cap a breach actually crossed, and where that cap is declared.

    A module can be governed by a source-declared ``*_SIZE_GOVERNANCE_CAP``
    tighter than its ``module_size_limits.json`` entry.  Reporting the JSON cap
    for such a breach prints an arithmetically false comparison (``1979 >
    2250``) and sends the reader to the wrong file to repair it.  Gates use
    this so a blocked reader learns which limit bound and which constant moves.
    """
    if effective_cap is None:
        return f"{module_key} ({count} lines, no cap resolved)"
    if json_cap is not None and effective_cap < json_cap:
        source = governance_source_relpath(section, module_key) or "the module itself"
        return (
            f"{module_key} ({count} > {effective_cap}, the governance cap "
            f"declared in {source}; the module_size_limits.json cap of "
            f"{json_cap} is NOT the binding limit)"
        )
    return f"{module_key} ({count} > {effective_cap})"


def governing_cap_repair(
    section: str, module_key: str, new_limit: int
) -> GoverningCapRepair | None:
    """Return the source-constant edit paired with a governed JSON cap bump.

    API callers use this complete instruction to keep the agent-facing repair
    contract coherent: governed modules must update both the JSON cap and the
    declared source constant in the same task.
    """
    governing = _GOVERNING_CAPS.get((section, module_key))
    if governing is None:
        return None
    return GoverningCapRepair(
        source_path=governing["source_path"],
        constant_name=governing["constant_name"],
        new_limit=new_limit,
    )


def rewrite_governing_cap_constant(
    section: str, module_key: str, source_text: str, new_cap: int
) -> str:
    """Return ``source_text`` with its declared governing constant updated.

    A mismatch is a governance configuration error, not permission to update
    only the JSON cap, so this fails closed rather than writing incoherent
    artifacts.  Callers use the result in the same staged write / Git commit as
    the cap JSON and history ledger.
    """
    governing = _GOVERNING_CAPS.get((section, module_key))
    if governing is None:
        return source_text
    constant = re.escape(governing["constant_name"])
    pattern = re.compile(
        rf"^(?P<prefix>{constant}\s*:\s*int\s*=\s*)\d+(?P<suffix>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    rewritten, replacements = pattern.subn(
        rf"\g<prefix>{new_cap}\g<suffix>", source_text
    )
    if replacements != 1:
        raise ValueError(
            f"governing cap constant {governing['constant_name']} for "
            f"{section}/{module_key} must occur exactly once (found {replacements})"
        )
    return rewritten


def prune_ledger_for_current_caps(ledger_data: dict, caps_data: dict) -> dict:
    """Drop ledger history for cap keys that no longer exist in ``caps_data``.

    Deleting a source file must remove both its cap and its obsolete historical
    evidence.  This works even in a shallow checkout, where a full git-ledger
    rebuild deliberately preserves existing records.
    """
    result = dict(ledger_data) if isinstance(ledger_data, dict) else {}
    history: dict[str, dict] = {}
    raw_history = result.get("history") or {}
    for section, records in raw_history.items():
        if not isinstance(records, dict):
            continue
        current = caps_data.get(section)
        if not isinstance(current, dict):
            continue
        kept = {key: value for key, value in records.items() if key in current}
        if kept:
            history[section] = kept
    result["history"] = history
    return result


STALE_CAP_HEADROOM: int = 500
STALE_CAP_MULTIPLIER: int = 2


def stale_cap_limit(actual_line_count: int) -> int:
    """Return the highest meaningful cap for a file of this size."""
    if actual_line_count < 0:
        raise ValueError("actual_line_count must be non-negative")
    return max(
        actual_line_count + STALE_CAP_HEADROOM,
        actual_line_count * STALE_CAP_MULTIPLIER,
    )


def is_breach(
    count: int | None,
    cap: int | None,
    *,
    base_count: int | None = None,
) -> bool:
    """True when a count is a new or grown breach of a cap.

    A record is in breach only when BOTH ``count`` and ``cap`` are present AND
    ``count > cap``. A file absent at the ref (``count is None``) is never a
    breach — a missing file cannot have grown past its cap. When ``base_count``
    is supplied, an otherwise-breaching record is inherited when its current
    count is no larger than the baseline; missing baseline evidence is treated
    as introduced (fail closed). This optional baseline argument keeps the
    absolute breach and introduced-vs-inherited attribution rules in one
    dependency-free primitive for every evaluator.
    """
    return (
        count is not None
        and cap is not None
        and count > cap
        and (base_count is None or count > base_count)
    )


def eligible_baseline_count(
    base_count: int | None,
    base_effective_cap: int | None,
) -> int | None:
    """Return baseline evidence only when the base breached its own cap.

    Baseline attribution is valid only for a file that was already over the
    effective cap in the base tree. Keeping this eligibility rule next to
    :func:`is_breach` prevents evaluators from accepting a clean base as
    evidence that a candidate breach was inherited.
    """
    return base_count if is_breach(base_count, base_effective_cap) else None


def resolve_effective_cap(
    module_key: str,
    section: str,
    json_cap: int | None,
    repo_root: str | None = None,
    *,
    governance_caps: list[int] | None = None,
) -> int | None:
    """Fold a source-declared governance cap into the cap the breach test uses.

    Previously the ``*_SIZE_GOVERNANCE_CAP`` a module declares was read only
    inside :func:`bump_size_limit` (the ``--fix`` auto-bump path) and never
    consulted by the breach comparison itself — a module sitting between its
    governance cap and a looser JSON cap, or with a governance cap and no
    JSON entry at all, raised nothing. This is the single point where "which
    number actually gates a breach" is decided: the tighter of the two when
    both exist, the governance cap alone when there is no JSON entry, and the
    JSON cap unchanged when the module declares no governance cap.
    ``governance_caps``, when passed (even ``[]``), is used AS-IS instead of
    reading the local tree — pass ``governance_caps_from_source_text(ref_text)``
    to gate against a specific ref instead of the local tree.
    """
    governing_caps = (
        governance_caps
        if governance_caps is not None
        else _governing_caps_for_module(module_key, section, repo_root)
    )
    if not governing_caps:
        return json_cap
    governance_cap = min(governing_caps)
    if json_cap is None:
        return governance_cap
    return min(json_cap, governance_cap)


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
    # A real commit SHA, or ``UNATTRIBUTED_COMMIT_SHA`` for a cap bump staged
    # before the commit that will contain it exists.
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


# A cap file and its ledger are sometimes committed together, so the commit SHA
# cannot be known while the tree is being assembled.  This explicit marker
# replaces the old empty-string record and is never eligible for pruning.
UNATTRIBUTED_COMMIT_SHA = "unattributed"

# Statuses returned by :func:`missing_ledger_bump_records`.  The two cases carry
# very different severity and MUST NOT be collapsed:
#
# ``LEDGER_BUMP_ABSENT``
#     The ledger holds no record at all for a cap raise that HEAD's own ancestry
#     proves happened.  That is genuine ledger drift — the deployed backend's
#     chronic-offender classification is missing a bump it should count — and is
#     a legitimate hard failure.
# ``LEDGER_BUMP_UNATTRIBUTED``
#     The ledger DOES hold a matching record for the pair, but it still carries
#     the ``UNATTRIBUTED_COMMIT_SHA`` (or legacy empty-string) placeholder that
#     :func:`ledger_with_pending_bumps` necessarily writes when the bump is
#     staged before its commit exists.  The bump is recorded and counted; only
#     the provenance is pending.  This is a normal, self-inflicted consequence
#     of every rollup auto-bump, so it is a reconcile HINT, never a hard failure
#     — otherwise each auto-bump would turn ``./check`` red fleet-wide until an
#     operator manually ran ``--fix``.
LEDGER_BUMP_ABSENT = "absent"
LEDGER_BUMP_UNATTRIBUTED = "unattributed_placeholder"


# Module-level TTL cache: (module_key, section, repo_root) -> (outcome,
# monotonic_time) where ``outcome`` is either a ``list[BumpRecord]`` (read
# succeeded) or a ``BumpHistoryUnavailableError`` (read failed). Failures are
# cached too so a broken-git reconcile cycle does not re-shell (and re-timeout)
# per call.  The RESOLVED REPO ROOT is part of the key: two roots legitimately
# answer the same ``(module_key, section)`` differently, so keying on the pair
# alone let whichever root ran first within the TTL answer for the other — a
# fixture read poisoning the ambient entry, or the reverse.
_bump_history_cache: dict[
    tuple[str, str, str], tuple[list[BumpRecord] | BumpHistoryUnavailableError, float]
] = {}
_CACHE_TTL_SECONDS: float = 60.0

# Per-ROOT memo for the git PROBES the history read performs.  Both probes
# answer a question about the CHECKOUT (which ref scopes the mainline ancestry;
# does that ancestry cross a shallow boundary), not about any one module, but
# ``_bump_history_cache`` is keyed per (module_key, section, root) — so a caller
# that classifies N modules against ONE root re-shelled both probes N times.
#
# Measured cost of that gap: the reaper's near-cap sweep
# (``app.metrics_integrity_sweep.check_near_cap_size_warning``) calls
# :func:`parse_bump_history` once per near-cap module — 48 modules today — and
# each call spawned SIX ``git`` processes (1 ``rev-parse --git-path``, 4
# ``rev-parse --verify`` for the mainline candidates, 1 ``merge-base
# --is-ancestor``).  288 serial process spawns inside one
# ``asyncio.to_thread``: 9.3s on an idle dev box, 16.1s inside a CI shard
# container against that lane's 30s per-test budget, which red-lit promotion
# PR #17977 when ordinary host jitter pushed it over.  With the memo the same
# sweep spawns 6 processes total.
#
# The RESOLVED root is the key (never the caller's ``None``): ``_REPO_ROOT`` is
# monkeypatched by tests and by ``--fix --repo-root``, so keying on the raw
# argument would let one root's answer serve another — the same poisoning
# ``_bump_history_cache`` documents above.  Same TTL as the history cache: both
# describe "what this checkout looks like right now".
_git_probe_cache: dict[tuple[str, str], tuple[object, float]] = {}

_ProbeT = TypeVar("_ProbeT")


def _probe_cached(kind: str, root: str, compute: Callable[[], _ProbeT]) -> _ProbeT:
    """Return ``compute()`` for ``(kind, root)``, memoised for the cache TTL."""
    key = (kind, root)
    now = time.monotonic()
    cached = _git_probe_cache.get(key)
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]  # type: ignore[return-value]
    value = compute()
    _git_probe_cache[key] = (value, now)
    return value


# Repo root: backend/app/ -> ../../
_SOURCE_TREE_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_REPO_ROOT = "/repo" if os.path.isdir("/repo/backend/app") else _SOURCE_TREE_ROOT
_TESTING_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "testing"))
_MODULE_SIZE_LIMITS_RELPATH = "backend/testing/module_size_limits.json"
_MODULE_SIZE_LIMITS_PATH = os.path.join(_TESTING_DIR, "module_size_limits.json")

# Every ``git log -p`` / ``git show`` read of the caps file MUST render whole-
# file context.  Cap attribution needs the enclosing ``"<section>": {`` opener
# (the same module basename lives in more than one section), and with git's
# default three lines of context that opener is almost never inside the hunk —
# git's funcname heuristic finds nothing usable in JSON either, since no line
# of it starts with an identifier character.  git clamps the value to the file
# size, so this asks for "the whole file" without hard-coding a length.
# Measured on the real history (213 commits touching the file): the default
# ``-U3`` stream is ~330KB, the full-context stream ~3.5MB and ~1s to produce.
# That is affordable ONLY because callers parse the stream ONCE
# (:func:`_parse_git_log_bump_records`) instead of re-scanning it per cap key.
_FULL_CONTEXT_DIFF_ARG = "-U100000"

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


def _is_git_checkout(repo_root: str) -> bool:
    """Return whether ``repo_root`` has ordinary or linked-worktree Git data."""
    git_marker = os.path.join(repo_root, ".git")
    # A linked worktree uses a .git *file* pointing at the common Git dir.
    return os.path.isdir(git_marker) or os.path.isfile(git_marker)


def _git_reads_history_here(repo_root: str | None = None) -> bool:
    """True only when git is on PATH AND the selected root is a git checkout.

    These are the two structural preconditions the deployed backend layout
    fails: the production ``app`` stage has no git binary, and under WORKDIR
    /app ``_REPO_ROOT`` resolves to ``/`` (no ``.git``; the image also excludes
    it). When either is false the git evidence source is unreachable and
    :func:`parse_bump_history` reads the committed ledger mirror instead.
    """
    root = repo_root or _REPO_ROOT
    return shutil.which("git") is not None and _is_git_checkout(root)


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
    return _probe_cached(
        "mainline_ref", root, lambda: _mainline_git_ref_uncached(root)
    )


def _mainline_git_ref_uncached(root: str) -> str:
    """Uncached body of :func:`_mainline_git_ref` — up to four ``git`` spawns."""
    for candidate in _MAINLINE_BRANCH_CANDIDATES:
        try:
            # safe.directory: keep the probe working when the checkout is bound
            # into a container as another user (CI's /repo mount) — without it
            # every candidate dies with exit 128 and the scope silently falls
            # back to HEAD.
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={root}",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    candidate,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                cwd=root,
            )
        except Exception:  # noqa: BLE001 — a probe; unresolved ref falls through
            continue
        if result.returncode == 0:
            return candidate
    return "HEAD"


def _git_shallow_file_path(repo_root: str) -> str:
    """Resolve Git's shallow-boundary file for clones and linked worktrees."""
    legacy_path = os.path.join(repo_root, ".git", "shallow")
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "rev-parse",
                "--git-path",
                "shallow",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — legacy path still covers ordinary clones
        return legacy_path
    resolved = result.stdout.strip() if result.returncode == 0 else ""
    if not resolved:
        return legacy_path
    return resolved if os.path.isabs(resolved) else os.path.join(repo_root, resolved)


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
    return _probe_cached(
        "history_truncated", root, lambda: _git_history_is_truncated_uncached(root)
    )


def _git_history_is_truncated_uncached(root: str) -> bool:
    """Uncached body of :func:`_git_history_is_truncated`."""
    shallow_path = _git_shallow_file_path(root)
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
            # ``-c safe.directory``: CI binds the checkout into a root container
            # while the runner user owns the files, so without it git dies with
            # "dubious ownership" (exit 128) and a shallow checkout would read
            # as NOT truncated — the exact fail-open this helper must not have.
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={root}",
                    "merge-base",
                    "--is-ancestor",
                    boundary,
                    ref,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                cwd=root,
            )
        except Exception:  # noqa: BLE001 — probe unrunnable: fail closed below
            return True
        if result.returncode == 0:
            # A shallow boundary commit is in the mainline ancestry, so the
            # mainline history is truncated.
            return True
        if result.returncode != 1:
            # ``--is-ancestor`` answers only 0 (yes) or 1 (no); anything else
            # (128 dubious-ownership, unresolvable ref) means the probe could
            # not evaluate the boundary at all. A shallow file IS present, so
            # treat unprovable as truncated — fail closed, never open.
            return True
    return False


def parse_bump_history(
    module_key: str, section: str = "backend", repo_root: str | None = None
) -> list[BumpRecord]:
    """Return the cap-bump history for ``module_key`` in ``repo_root``.

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

    ``repo_root`` selects WHICH checkout's history is read (default: ambient
    ``_REPO_ROOT``); callers classifying a breach in a specific tree must pass
    it.  Without it a ``--fix`` against a fixture root read the *ambient*
    repository's history and the verdict tracked whatever checkout was on disk.
    """
    root = repo_root or _REPO_ROOT
    cache_key = (module_key, section, root)
    now = time.monotonic()
    cached = _bump_history_cache.get(cache_key)
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        outcome = cached[0]
        if isinstance(outcome, BumpHistoryUnavailableError):
            raise outcome
        return outcome

    records = None
    if _git_reads_history_here(root) and not _git_history_is_truncated(root):
        # Complete checkout: read the bump history from the mainline git
        # ancestry.  A real git failure here is genuine evidence loss and stays
        # fail-closed (raised, not degraded to "no bump history").
        try:
            records = _read_git_bump_history(module_key, root, section=section)
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


def _read_git_bump_history(
    module_key: str, repo_root: str | None = None, *, section: str
) -> list[BumpRecord] | None:
    """Read bump history from git, or ``None`` when git is structurally absent.

    ``section`` names the enclosing object in ``module_size_limits.json``; it
    is part of a cap's identity because the same module basename exists in
    more than one section (``test_local_runner.py`` is both a ``testing`` and
    a ``scripts`` cap, on two different real files).

    ``None`` (NOT an error) signals the deployed layout: no git binary on PATH
    or ``repo_root`` (default ``_REPO_ROOT``) is not a git checkout.  The caller
    then falls back to the committed ledger mirror.

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
    root = repo_root or _REPO_ROOT
    if not _git_reads_history_here(root):
        return None
    if _git_history_is_truncated(root):
        raise BumpHistoryUnavailableError(
            f"git history is truncated (shallow checkout at {root}); "
            f"cannot read bump history for {module_key!r}"
        )
    try:
        # ``-c safe.directory``: CI binds the checkout into a ROOT container
        # the runner user owns (``-v "$PWD:/repo:ro"``, no ``--user``), so
        # without it this read dies with "dubious ownership" (exit 128) while
        # :func:`_mainline_git_ref`'s already-hardened probe still resolves a
        # real mainline ref.  A non-zero ``git log`` is genuine evidence loss,
        # so that asymmetry made every chronic-offender check in the container
        # refuse its cap bump and ``--fix`` exit 1.  Probe and read must carry
        # the SAME hardening; the argv tests pin them together.
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root}",
                "log",
                _mainline_git_ref(root),
                "--format=COMMIT %H %aI",
                "-p",
                _FULL_CONTEXT_DIFF_ARG,
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            cwd=root,
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
    return _parse_git_diff_output(result.stdout, module_key, section)


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
    ledger_data: dict,
    pending: list[
        tuple[str, str, int, int]
        | tuple[str, str, int, int, str, str]
    ],
) -> dict:
    """Return a copy of ``ledger_data`` with ``pending`` bumps reconciled.

    Four-field entries represent an uncommitted bump and receive the explicit
    ``UNATTRIBUTED_COMMIT_SHA`` marker. Six-field entries add observed git
    provenance for an already-committed bump; that SHA/date is preserved and
    upgrades an existing unattributed record for the same cap pair.
    """
    result = dict(ledger_data) if isinstance(ledger_data, dict) else {}
    history: dict = {}
    for section, section_data in (result.get("history") or {}).items():
        history[section] = dict(section_data) if isinstance(section_data, dict) else {}
    now_iso = datetime.now(timezone.utc).isoformat()
    for item in pending:
        if len(item) == 4:
            section, key, old_cap, new_cap = item
            commit_sha = UNATTRIBUTED_COMMIT_SHA
            commit_date = now_iso
        elif len(item) == 6:
            section, key, old_cap, new_cap, commit_sha, commit_date = item
        else:
            raise ValueError(f"pending cap bump must contain 4 or 6 fields: {item!r}")
        section_records = history.setdefault(section, {})
        records = list(section_records.get(key) or [])
        matching = [
            record
            for record in records
            if record.get("old_cap") == old_cap and record.get("new_cap") == new_cap
        ]
        if matching:
            if len(item) == 6 and matching[0].get("commit_sha") in {
                "",
                UNATTRIBUTED_COMMIT_SHA,
            }:
                matching[0]["commit_sha"] = commit_sha
                matching[0]["commit_date"] = commit_date
        else:
            records.append(
                {
                    "commit_sha": commit_sha,
                    "commit_date": commit_date,
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
    pending: list[
        tuple[str, str, int, int]
        | tuple[str, str, int, int, str, str]
    ] | None = None,
    repair_targets: set[tuple[str, str]] | None = None,
    repo_root: str | None = None,
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
    A non-None ``repair_targets`` restricts this write to those ledger keys,
    preserving all unrelated existing entries during an attributed task repair.

    When git is absent or the checkout is truncated (the mainline ancestry
    crosses a ``.git/shallow`` boundary — a shallow task-VM clone, CI's depth-1
    checkout — so ``git log`` would silently return a partial history), the
    existing ledger is preserved rather than regenerated — a truncated ``git
    log`` must never truncate the mirror.  In a complete checkout the mirror is
    rebuilt from the mainline ancestry, and only if that would not drop a
    record the existing ledger already holds for a current key is the mirror
    replaced outright (the merge path keeps both).  ``git log`` itself never
    runs on a truncated checkout.  On ANY checkout where git is available —
    including truncated ones — the mirror is then self-healed by pruning only
    records that positive git evidence proves fabricated
    (:func:`phantom_ledger_records`). Missing or otherwise unverifiable evidence
    is preserved, never treated as proof of fabrication. ``caps_json_path`` /
    ``ledger_path``
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
    # ``repo_root``/``-c safe.directory``: this read FAILS OPEN (a git error
    # falls through to "preserve the existing mirror"), so unhardened it would
    # rebuild the ledger from ZERO evidence inside CI's root-container ``/repo``
    # bind mount and never red anything.  Silent, unlike the fail-closed read in
    # :func:`_read_git_bump_history` -- which is why it must be hardened too.
    root = repo_root or _REPO_ROOT
    git_available = _git_reads_history_here(root)
    if git_available and not _git_history_is_truncated(root):
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={root}",
                    "log",
                    _mainline_git_ref(root),
                    "--format=COMMIT %H %aI",
                    "-p",
                    _FULL_CONTEXT_DIFF_ARG,
                    "--",
                    _MODULE_SIZE_LIMITS_RELPATH,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                cwd=root,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to append-only below
            result = None
            _ = exc
        if result is not None and result.returncode == 0:
            git_output = result.stdout

    # The caps JSON is read ONCE, and whether the read succeeded is tracked
    # separately from its contents: "the caps file could not be read" must never
    # be mistaken for "no caps exist".  An unreadable/malformed caps file makes
    # both the git regeneration and the deleted-cap prune below unable to tell a
    # current key from a deleted one, so both are skipped and the existing
    # mirror is preserved (fail-closed).
    try:
        with open(caps_path, encoding="utf-8") as f:
            caps_data = json.load(f)
        caps_readable = True
    except (OSError, ValueError):
        caps_data = {}
        caps_readable = False

    if git_output is not None and caps_readable:
        caps_keys: dict[str, set[str]] = {}
        git_histories: dict[str, dict[str, list[BumpRecord]]] = {}
        # ONE pass over the (full-context, multi-megabyte) log for ALL keys.
        # Parsing per key would re-scan the whole stream several hundred times.
        git_records = _parse_git_log_bump_records(git_output)
        for section, keys in caps_data.items():
            if not isinstance(keys, dict):
                continue
            caps_keys[section] = set(keys)
            section_records: dict[str, list[BumpRecord]] = {}
            for key in keys:
                recs = git_records.get((section, key))
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
        # git entirely unavailable, the checkout is truncated, or the caps JSON
        # is unreadable — preserve the existing mirror (a truncated ``git log``,
        # like an unreadable caps file, must never truncate it).
        base_history = existing_history

    # A deleted cap has no future breach to classify.  Prune its mirror record
    # before any merge-preservation logic can carry it forward in a shallow
    # checkout.  Only ever prune against caps we actually read: an unreadable
    # caps file would otherwise look like "every cap was deleted" and wipe the
    # whole ledger.
    if caps_readable:
        base_history = prune_ledger_for_current_caps(
            {"history": base_history}, caps_data
        )["history"]

    if git_available:
        # Self-heal: prune ledger records positive mainline evidence proves
        # fabricated (a wrong old_cap, a decrease mis-filed as a bump, or a
        # non-ancestor commit). Without this the
        # _git_would_drop_ledger_records merge safety net would preserve them
        # FOREVER — a phantom pair is by definition absent from git's bump set,
        # so regeneration would "drop" it and the merge path would merge it
        # straight back in.  Unlike the full-history regeneration above, this
        # runs on truncated checkouts too (every CI shard and agent VM is
        # shallow). phantom_ledger_records skips every record whose provenance
        # cannot be corroborated; only positive contradictory evidence is
        # returned for pruning.
        phantoms = phantom_ledger_records(
            repo_root=_REPO_ROOT,
            ledger_path=out_path,
            history=existing,
        )
        if phantoms:
            base_history = _prune_phantom_records(base_history, phantoms)

    if repair_targets is not None:
        # A target-scoped cap repair still needs the canonical ledger mutation
        # for the cap it bumped, but must not use that necessary write as an
        # opportunity to regenerate, prune, or attribute other keys. Preserve
        # non-target entries from the original mirror after all normal policy
        # calculations, then retain only calculated entries for the targets.
        restricted_history: dict = {}
        for section, records in existing_history.items():
            if not isinstance(records, dict):
                continue
            preserved = {
                key: value
                for key, value in records.items()
                if (section, key) not in repair_targets
            }
            if preserved:
                restricted_history[section] = preserved
        for section, key in repair_targets:
            records = base_history.get(section, {})
            if key in records:
                restricted_history.setdefault(section, {})[key] = records[key]
        base_history = restricted_history

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
    # newline="": this ledger is a tracked file, and platform newline
    # translation on a Windows --fix would rewrite every line as CRLF.
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=True)
        f.write("\n")
    return ledger


def _head_reachable_cap_commits(repo_root: str) -> set[str] | None:
    """Return the caps-file commits in ``HEAD``'s own ancestry.

    Mainline moves on while a task branch is open, so a cap bump that landed on
    automain *after* the branch forked is genuinely absent from the branch's
    ledger — the branch does not contain the commit and cannot have the record.
    Scoping the git->ledger comparison to this set is what separates real ledger
    drift (a bump the branch DOES contain but never mirrored) from ordinary
    branch/mainline skew, which must never turn the guard red.

    Returns ``None`` when the ancestry cannot be determined; callers treat that
    as "prove nothing" rather than risking a false breach.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "rev-list",
                "HEAD",
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=repo_root,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def missing_ledger_bump_records(
    *,
    repo_root: str | None = None,
    caps_json_path: str | None = None,
    ledger_path: str | None = None,
) -> list[tuple[str, str, int, int, str, str, str]]:
    """Return cap raises in HEAD's own ancestry the ledger does not fully record.

    This is the forward half of ledger coherence (git -> ledger).  A shallow
    history is still useful here: every bump it *does* expose is positive
    evidence that the mirror must contain the pair.  Callers preserve the
    existing ledger and append only these proven pairs, never treating absent
    shallow history as permission to delete an older record.

    Only bumps whose commit is reachable from ``HEAD`` are reported.  A mainline
    bump that landed after this branch forked is not drift the branch can fix —
    reporting it would fail ``check_size_limits.py`` (and therefore ``./check``)
    on every branch that merely forked early, and the ``--fix`` remediation
    would write mainline-owned records into each task branch's ledger.

    Each entry is ``(section, key, old_cap, new_cap, commit_sha, commit_date,
    status)`` where ``status`` is :data:`LEDGER_BUMP_ABSENT` (no ledger record
    for the pair at all — real drift, safe for a caller to fail on) or
    :data:`LEDGER_BUMP_UNATTRIBUTED` (a matching record exists but still holds
    the pre-commit placeholder SHA — only the provenance is pending, so callers
    must treat it as a reconcile hint, NOT a failure).  Both statuses are
    repairable by the same ``--fix`` rebuild; they differ only in severity.
    Collapsing them is what made every rollup auto-bump break ``./check``
    fleet-wide until an operator manually reconciled the ledger.
    """
    root = repo_root or _REPO_ROOT
    caps_path = caps_json_path or _MODULE_SIZE_LIMITS_PATH
    out_path = ledger_path or _CAP_BUMP_HISTORY_LEDGER_PATH
    if not _git_reads_history_here(root):
        return []
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root}",
                "log",
                _mainline_git_ref(root),
                "--format=COMMIT %H %aI",
                "-p",
                _FULL_CONTEXT_DIFF_ARG,
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=root,
        )
        if result.returncode != 0:
            return []
        with open(caps_path, encoding="utf-8") as caps_file:
            caps_data = json.load(caps_file)
        with open(out_path, encoding="utf-8") as ledger_file:
            ledger_data = json.load(ledger_file)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []

    # Restrict the mainline evidence to this checkout's own ancestry.  Without
    # this, a bump merged to mainline after the branch forked reads as ledger
    # drift the branch caused, and the guard goes red on unrelated work.
    reachable = _head_reachable_cap_commits(root)
    if reachable is None:
        return []

    # ONE pass over the (full-context, multi-megabyte) log for ALL keys: the
    # loop below walks several hundred caps, and re-parsing the stream per key
    # would put gigabytes of regex work inside a single guard call.
    git_records = _parse_git_log_bump_records(result.stdout)

    history = ledger_data.get("history", {}) if isinstance(ledger_data, dict) else {}
    global_start = min(
        (
            _parse_iso(record["commit_date"])
            for sections in history.values()
            if isinstance(sections, dict)
            for records in sections.values()
            if isinstance(records, list)
            for record in records
            if isinstance(record, dict) and record.get("commit_date")
        ),
        default=None,
    )
    missing: list[tuple[str, str, int, int, str, str, str]] = []
    for section, keys in caps_data.items():
        if not isinstance(keys, dict):
            continue
        section_history = history.get(section, {}) if isinstance(history, dict) else {}
        for key in keys:
            records = section_history.get(key, []) if isinstance(section_history, dict) else []
            window_start = min(
                (
                    _parse_iso(record["commit_date"])
                    for record in records
                    if isinstance(record, dict) and record.get("commit_date")
                ),
                default=global_start,
            )
            for record in git_records.get((section, key), []):
                if record["commit_sha"] not in reachable:
                    # Mainline-only bump: this branch does not contain the
                    # commit, so its ledger cannot be expected to record it.
                    continue
                pair = (record["old_cap"], record["new_cap"])
                matching_records = [
                    ledger_record
                    for ledger_record in records
                    if (
                        ledger_record.get("old_cap"),
                        ledger_record.get("new_cap"),
                    )
                    == pair
                ]
                if matching_records and matching_records[0].get("commit_sha") not in {
                    "",
                    UNATTRIBUTED_COMMIT_SHA,
                }:
                    continue
                # A matching unattributed pair is exactly the record that must
                # be upgraded with the git provenance we just observed.  Its
                # placeholder date may have been written after the real
                # mainline commit, so the ordinary per-key timestamp window
                # must not hide that pair from reconciliation.
                if (
                    not matching_records
                    and window_start is not None
                    and _parse_iso(record["commit_date"]) < window_start
                ):
                    continue
                # The bump IS in the ledger whenever a matching pair exists —
                # only its provenance is still the pre-commit placeholder, so
                # this is a pending attribution rather than missing evidence.
                status = (
                    LEDGER_BUMP_UNATTRIBUTED if matching_records else LEDGER_BUMP_ABSENT
                )
                missing.append(
                    (
                        section,
                        key,
                        record["old_cap"],
                        record["new_cap"],
                        record["commit_sha"],
                        record["commit_date"],
                        status,
                    )
                )
    return missing


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


def _prune_phantom_records(
    base_history: dict,
    phantoms: "list[tuple[str, str, int, int, str, str]]",
) -> dict[str, dict[str, list[BumpRecord]]]:
    """Remove from ``base_history`` every record ``phantoms`` flags.

    ``phantoms`` is the output of :func:`phantom_ledger_records` — each entry
    ``(section, key, old_cap, new_cap, commit_sha, reason)`` identifies a
    record the mainline git history cannot justify.  Records are matched on all
    five identifying fields so a real bump sharing a pair with a phantom (same
    key/pair at a different commit) is never dropped.
    """
    phantom_keys = {
        (section, key, old_cap, new_cap, sha)
        for (section, key, old_cap, new_cap, sha, _reason) in phantoms
    }
    pruned: dict[str, dict[str, list[BumpRecord]]] = {}
    for section, keys in base_history.items():
        if not isinstance(keys, dict):
            continue
        section_out: dict[str, list[BumpRecord]] = {}
        for key, records in keys.items():
            if not isinstance(records, list):
                continue
            kept = [
                r
                for r in records
                if (
                    section,
                    key,
                    r.get("old_cap"),
                    r.get("new_cap"),
                    r.get("commit_sha"),
                )
                not in phantom_keys
            ]
            if kept:
                section_out[key] = kept
        if section_out:
            pruned[section] = section_out
    return pruned


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


def _split_git_log_chunks(output: str) -> list[tuple[str, str, str]]:
    """Split ``git log -p`` output into ``(commit_sha, commit_date, diff_text)`` chunks.

    Extracted from :func:`_parse_git_diff_output` so callers that need every
    commit's raw diff — the phantom-ledger check, which verifies each ledger
    record against its own commit — share the same commit-splitting instead of
    re-deriving it.  A commit header is ``COMMIT <sha> <date>``; everything
    after it until the next header is that commit's diff text.
    """
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
    return chunks


# ``module_size_limits.json`` maps a SECTION (``backend``, ``testing``,
# ``scripts``, ...) to ``{module_basename: cap}``, and the SAME basename
# legitimately exists in two sections pointing at two different real files
# (``test_local_runner.py`` lives in both ``testing/`` and ``scripts/``).  A
# diff line alone therefore does NOT identify a cap: attribution needs the
# enclosing section object.  These two scanners recover it from the diff
# itself; every parser below keys its result by ``(section, key)``, never by
# ``key`` alone.  Section-blind attribution was wrong in BOTH directions: it
# reported a bump of ``testing/X.py`` as unrecorded drift in ``scripts/X.py``
# (a false failure whose ``--fix`` wrote a transition that never happened),
# and it let a real bump in one section be "justified" by an unrelated ledger
# record filed under another section with a coincidentally equal cap pair.
#
# Order matters: a section opener ends in ``{`` and an entry ends in a number,
# so the opener must be tested FIRST.  Indentation is deliberately loose
# because historical revisions of the file need not match today's formatting.
_SECTION_OPEN_RE = re.compile(r'^([ +-])\s*"([^"]+)"\s*:\s*\{')
_ENTRY_RE = re.compile(r'^([ +-])\s*"([^"]+)"\s*:\s*(\d+)')


class DiffBumpPairs(NamedTuple):
    """Section-attributed cap bumps found in ONE commit's diff.

    ``by_section_key`` maps ``(section, key)`` to the increase pairs the diff
    proves.  ``unattributed_keys`` holds every key whose enclosing section
    could not be determined from the diff text — evidence that exists but
    cannot be placed.  Callers must treat those as ABSENT evidence (skip),
    never as a verdict: skipping is the safe direction for both halves of the
    ledger guard.  For :func:`missing_ledger_bump_records` (git -> ledger) it
    avoids raising a ``[ledger-missing]`` failure for a bump we cannot place,
    and for :func:`phantom_ledger_records` (ledger -> git) it avoids
    condemning a real record as fabricated because its section was invisible.
    """

    by_section_key: dict[tuple[str, str], set[tuple[int, int]]]
    unattributed_keys: frozenset[str]


def _scan_diff_bumps(diff_text: str) -> DiffBumpPairs:
    """Scan one commit's diff for section-attributed cap increases.

    Two independent section cursors are tracked, an OLD-side one and a
    NEW-side one: a context line (``' '``) advances both, a ``'-'`` line only
    the old cursor and a ``'+'`` line only the new one.  A removed entry is
    attributed to the old-side section and an added entry to the new-side
    section, which stays exact even when a section is added, removed or
    renamed in the same commit.

    Only an INCREASE is a bump (``new > old``); a decrease or reset is not,
    which is why a decrease mis-filed in the ledger is a phantom.  At most one
    pair per ``(section, key)`` per commit is produced — the last ``-``/``+``
    values seen — matching the historical per-commit behaviour.

    The diff must be rendered with FULL file context (see
    :data:`_FULL_CONTEXT_DIFF_ARG`); with git's default three context lines the
    enclosing ``"<section>": {`` opener is almost never inside the hunk and
    git's funcname heuristic finds nothing usable in JSON, so nearly every
    bump would land in ``unattributed_keys``.
    """
    old_section: str | None = None
    new_section: str | None = None
    seen: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    unattributed: set[str] = set()

    for line in diff_text.split("\n"):
        opener = _SECTION_OPEN_RE.match(line)
        if opener is not None:
            marker, name = opener.group(1), opener.group(2)
            if marker != "+":
                old_section = name
            if marker != "-":
                new_section = name
            continue
        entry = _ENTRY_RE.match(line)
        if entry is None:
            continue
        marker, key, raw_value = entry.group(1), entry.group(2), entry.group(3)
        if marker == " ":
            # An unchanged entry is context, not a bump.
            continue
        section = old_section if marker == "-" else new_section
        if section is None:
            # Fail open: evidence with no attributable section is reported as
            # ABSENT, never as a verdict in either direction.
            unattributed.add(key)
            continue
        value = int(raw_value)
        old_cap, new_cap = seen.get((section, key), (None, None))
        if marker == "-":
            seen[(section, key)] = (value, new_cap)
        else:
            seen[(section, key)] = (old_cap, value)

    by_section_key: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for section_key, (old_cap, new_cap) in seen.items():
        if old_cap is not None and new_cap is not None and new_cap > old_cap:
            by_section_key.setdefault(section_key, set()).add((old_cap, new_cap))
    return DiffBumpPairs(by_section_key, frozenset(unattributed))


def _parse_git_log_bump_records(
    output: str,
) -> dict[tuple[str, str], list[BumpRecord]]:
    """Parse a whole ``git log -p`` stream ONCE into ``(section, key) -> records``.

    Splits the output on ``COMMIT <sha> <date>`` header lines (via
    :func:`_split_git_log_chunks`), then attributes each commit's cap
    increases to their enclosing section (:func:`_scan_diff_bumps`).

    Parsing the stream once is load-bearing, not a micro-optimisation: the
    full-context diff of this file is multiple megabytes and its callers loop
    over several hundred cap keys.  Re-scanning per key would put gigabytes of
    regex work inside one guard call and blow the subprocess/CPU budget the
    checker runs under.

    Merge-duplicated bump pairs are collapsed per ``(section, key)``: the same
    cap change (old_cap -> new_cap) is frequently recorded twice — once on the
    commit that first introduced the new limit value (typically an
    integration-branch rollup) and again on a sibling/promotion commit that
    re-applied the identical value (e.g. a main-side "adopt automain" fix).
    Both diffs carry the exact same -/+ pair, so a naive parse would count one
    logical bump twice and inflate the chronic-offender window/growth
    triggers.  Rule: for each pair keep ONLY the record with the earliest
    commit_date — the commit that first introduced the new limit value — and
    drop the later duplicate(s).  git log emits commits newest-first, so the
    sibling/promotion duplicate appears BEFORE the commit that introduced it;
    the survivor must be chosen by TIMESTAMP, not by position.  Records are
    sorted by actual timestamp (not the ISO string, which mis-orders
    mixed-offset commits).  This keeps regeneration stable and idempotent: the
    surviving records depend only on the content of the history, not on which
    refs happen to be fetched or on git's traversal order.
    """
    earliest: dict[tuple[str, str], dict[tuple[int, int], BumpRecord]] = {}
    for sha, date, diff_text in _split_git_log_chunks(output):
        for section_key, pairs in _scan_diff_bumps(diff_text).by_section_key.items():
            slot = earliest.setdefault(section_key, {})
            for old_cap, new_cap in pairs:
                prior = slot.get((old_cap, new_cap))
                if prior is None or _parse_iso(date) < _parse_iso(
                    prior["commit_date"]
                ):
                    slot[(old_cap, new_cap)] = BumpRecord(
                        commit_sha=sha,
                        commit_date=date,
                        old_cap=old_cap,
                        new_cap=new_cap,
                    )
    return {
        section_key: sorted(
            records.values(), key=lambda r: _parse_iso(r["commit_date"])
        )
        for section_key, records in earliest.items()
    }


def _parse_git_diff_output(
    output: str, module_key: str, section: str
) -> list[BumpRecord]:
    """Return ``git log -p`` cap increases for ``module_key`` inside ``section``.

    ``section`` is REQUIRED: the same basename exists in more than one section
    of ``module_size_limits.json``, so a key alone cannot identify a cap.  See
    :func:`_parse_git_log_bump_records` for the collapse rule and
    :func:`_scan_diff_bumps` for the attribution rule.

    Callers that need more than one key out of the same output must call
    :func:`_parse_git_log_bump_records` ONCE and index the result instead of
    calling this per key — see that function's note on the parse budget.
    """
    return _parse_git_log_bump_records(output).get((section, module_key), [])


def _is_combined_git_diff(diff_text: str) -> bool:
    """Return whether ``diff_text`` is a merge commit's combined diff.

    Combined diffs use ``diff --cc``/``diff --combined`` headers and prefix
    hunk lines with one marker per parent.  They are readable, but they do not
    provide the single-parent ``- old/+ new`` evidence consumed by
    :func:`_scan_diff_bumps`; treating that parser miss as proof of a phantom
    would delete legitimate ledger records attributed to a merge SHA.
    """
    return any(
        line.startswith(("diff --cc ", "diff --combined "))
        for line in diff_text.splitlines()
    )


def _git_log_bump_map(
    output: str,
) -> dict[str, DiffBumpPairs]:
    """Build ``commit_sha -> DiffBumpPairs`` from ``git log -p`` output.

    The per-commit bump map the phantom-ledger check verifies ledger records
    against.  Pairs are keyed by ``(section, key)``: the same module basename
    exists in more than one section, so a per-key map would let a record filed
    under one section be corroborated by a bump that happened in another.

    Merge-duplicated pairs are NOT collapsed here (unlike
    :func:`_parse_git_log_bump_records`): each commit is verified on its own
    merits, and the same pair legitimately appearing in two commits justifies
    a ledger record pointing at either one.

    A commit is present in the map whenever its diff yielded either an
    attributed pair OR an unattributable key, so the caller can tell "this
    commit shows nothing for that cap" from "this commit's evidence could not
    be placed in a section".
    """
    result: dict[str, DiffBumpPairs] = {}
    for sha, _date, diff_text in _split_git_log_chunks(output):
        if _is_combined_git_diff(diff_text):
            # A merge's combined diff is not attributable to one parent and
            # must be adjudicated as absent evidence by phantom_ledger_records.
            continue
        scanned = _scan_diff_bumps(diff_text)
        if scanned.by_section_key or scanned.unattributed_keys:
            result[sha] = scanned
    return result


def _resolve_shas(shas: set[str], repo_root: str) -> dict[str, bool]:
    """Return ``{sha: is_resolvable}`` for a set of SHAs via ONE batch call.

    ``git cat-file --batch-check`` answers every SHA in a single subprocess
    instead of one ``git cat-file`` per record — the ledger holds hundreds of
    records and per-SHA probing would shell hundreds of times.
    """
    if not shas:
        return {}
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "cat-file",
                "--batch-check=%(objectname) %(objecttype)",
            ],
            input="\n".join(sorted(shas)) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — a probe; treat as unresolvable below
        return {}
    resolved: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        resolved[parts[0]] = len(parts) >= 2 and parts[1] != "missing"
    return resolved


def _commit_in_mainline_ancestry(sha: str, repo_root: str) -> bool | None:
    """True when ``sha`` is in the mainline ancestry, False when provably not.

    ``None`` means the probe could not answer (git error / unreadable ref) —
    the caller must then fail closed on a complete checkout and skip on a
    truncated one.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "merge-base",
                "--is-ancestor",
                sha,
                _mainline_git_ref(repo_root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — a probe; caller decides the fallback
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _git_show_file_diff(sha: str, repo_root: str) -> str | None:
    """Return the commit's diff of ``module_size_limits.json``, or ``None``.

    Used to verify a ledger record whose commit does not appear in the partial
    mainline ``git log`` (a beyond-boundary ancestor whose objects happen to be
    present in a shallow clone).  ``git show`` can render that commit's diff
    directly even though the log traversal stops at the shallow boundary.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "show",
                sha,
                _FULL_CONTEXT_DIFF_ARG,
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — a probe; caller decides the fallback
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_commit_parent_count(sha: str, repo_root: str) -> int | None:
    """Return a commit's parent count, or ``None`` when it cannot be read."""
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "show",
                "-s",
                "--format=%P",
                sha,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — unavailable provenance is not contradiction
        return None
    if result.returncode != 0:
        return None
    return len(result.stdout.split())


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _shallow_boundary_shas(repo_root: str) -> frozenset[str]:
    """Return every explicit shallow-clone boundary SHA, read ONCE.

    Git hides a shallow boundary's parent and renders ``git show`` as though
    the boundary were a root commit.  For ``module_size_limits.json`` that is
    a whole-file addition, so the ordinary ``old -> new`` pair parser cannot
    adjudicate a ledger record even when the cited commit made the real bump.
    The shallow file is positive evidence of that loss; an unreadable or
    absent shallow file is NOT treated as a boundary.

    Replaces a per-SHA predicate that re-resolved the shallow file path (a
    ``git rev-parse`` fork) and re-read the file for every SHA it was asked
    about.  The ledger holds hundreds of records, so the per-record form forked
    hundreds of times over one unchanging file.
    """
    try:
        with open(_git_shallow_file_path(repo_root), encoding="utf-8") as f:
            return frozenset(line.strip() for line in f if line.strip())
    except OSError:
        return frozenset()


def _mainline_ancestor_shas(ref: str, repo_root: str) -> frozenset[str] | None:
    """Every commit reachable from ``ref``, or ``None`` when git cannot answer.

    For a full 40-hex SHA, ``sha in <this set>`` is the batched equivalent of
    :func:`_commit_in_mainline_ancestry`: ``git merge-base --is-ancestor sha
    ref`` asks exactly whether ``sha`` is reachable from ``ref``, and both
    forms stop at the same shallow boundary.  One traversal answers every
    ledger record instead of forking ``merge-base`` — plus its nested
    :func:`_mainline_git_ref` probe — once per record.
    """
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo_root}", "rev-list", ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — a probe; the caller falls back per-SHA
        return None
    if result.returncode != 0:
        return None
    return frozenset(result.stdout.split())


def _ancestry_verdict(
    sha: str, ancestor_shas: frozenset[str] | None, repo_root: str
) -> bool | None:
    """Batched :func:`_commit_in_mainline_ancestry`, with a per-SHA fallback.

    Membership in the batched reachability set answers a full 40-hex SHA
    exactly.  An abbreviated SHA, or a batch git could not read, falls back to
    the single-SHA ``merge-base --is-ancestor`` probe so every verdict —
    including its ``None`` (the probe could not answer) polarity — is
    unchanged by the batching.
    """
    if ancestor_shas is not None and _FULL_SHA_RE.fullmatch(sha):
        return sha in ancestor_shas
    return _commit_in_mainline_ancestry(sha, repo_root)


def _batch_commit_parent_counts(
    shas: list[str], repo_root: str
) -> dict[str, int] | None:
    """Parent counts for many commits in ONE ``git log --no-walk`` fork.

    ``None`` means the batch could not be read; the caller then falls back to
    the per-SHA :func:`_git_commit_parent_count`, so a batch failure never
    changes a verdict.
    """
    if not shas:
        return {}
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "log",
                "--no-walk=unsorted",
                "--stdin",
                "--format=%H %P",
            ],
            input="\n".join(shas) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — a probe; the caller falls back per-SHA
        return None
    if result.returncode != 0:
        return None
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts:
            counts[parts[0]] = len(parts) - 1
    return counts


def _batch_commit_file_diffs(shas: list[str], repo_root: str) -> dict[str, str] | None:
    """``{sha: diff_text}`` for many commits in ONE ``git log -p`` fork.

    The same per-commit diff of ``_MODULE_SIZE_LIMITS_RELPATH`` that
    :func:`_git_show_file_diff` renders one fork at a time.  A commit that did
    not touch the file is absent from the mapping, which the caller reads as
    the empty diff ``git show`` would have rendered for it — the same verdict.
    ``None`` means the batch could not be read; the caller then falls back to
    the per-SHA helper.
    """
    if not shas:
        return {}
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root}",
                "log",
                "--no-walk=unsorted",
                "--stdin",
                "--format=COMMIT %H %aI",
                "-p",
                _FULL_CONTEXT_DIFF_ARG,
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            input="\n".join(shas) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=repo_root,
        )
    except Exception:  # noqa: BLE001 — a probe; the caller falls back per-SHA
        return None
    if result.returncode != 0:
        return None
    return {
        sha: diff_text
        for sha, _date, diff_text in _split_git_log_chunks(result.stdout)
        if sha
    }


def phantom_ledger_records(
    *,
    repo_root: str | None = None,
    ledger_path: str | None = None,
    history: dict | None = None,
) -> list[tuple[str, str, int, int, str, str]]:
    """Return ledger records the mainline git history cannot justify.

    Each returned entry is ``(section, key, old_cap, new_cap, commit_sha,
    reason)`` — a record in the cap-bump-history ledger that positive git
    evidence proves contradictory to the named commit. This is the REVERSE
    direction of the ``git ⊆ ledger`` guard: while that guard proves the mirror
    is not missing a git-recorded bump, this proves no record the mirror holds
    is fabricated — a wrong ``old_cap``, a decrease mis-filed as a bump, or a
    non-ancestor commit. Empty and explicitly unattributed records are always
    preserved because absent or unverifiable evidence cannot justify deletion.

    Runs on a truncated/shallow checkout too — every CI shard and agent VM — so
    the phantom check is not skipped exactly where the ledger most often
    drifts.  Records whose SHA resolves in the partial history ARE verified;
    only records the partial history cannot adjudicate are skipped:

    * a SHA that does not resolve on a shallow checkout (the object was never
      fetched) — skipping is provably safe there, because a fabricated SHA is
      indistinguishable from a genuine beyond-boundary bump when the object is
      gone; and
    * a beyond-boundary ancestor whose ancestry probe cannot answer.

    Even on a complete checkout, an unresolved or unreadable provenance check is
    preserved. A record is prunable only when the available diff or ancestry
    result positively contradicts it.

    ``history`` (the full ledger dict) defaults to the shipped ledger at
    ``ledger_path``; ``repo_root`` defaults to ``_REPO_ROOT``.
    """
    root = repo_root or _REPO_ROOT
    if not _git_reads_history_here(root):
        # No git checkout — nothing to verify the ledger against.  The ledger
        # is the only history source in the deployed layout by design.
        return []
    if history is None:
        out_path = ledger_path or _CAP_BUMP_HISTORY_LEDGER_PATH
        try:
            with open(out_path, encoding="utf-8") as f:
                ledger = json.load(f)
        except (OSError, ValueError):
            return []
    else:
        ledger = history
        out_path = ledger_path or _CAP_BUMP_HISTORY_LEDGER_PATH

    ref = _mainline_git_ref(root)
    if ref == "HEAD":
        # No mainline branch present (a feature-branch-only clone): the ref
        # helper fell back to HEAD, whose ancestry is not the ledger's source of
        # truth.  Same clear-skip the cap-raise guard uses — a phantom verdict
        # needs the mainline ancestry to compare against.
        return []

    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root}",
                "log",
                ref,
                "--format=COMMIT %H %aI",
                "-p",
                _FULL_CONTEXT_DIFF_ARG,
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=root,
        )
    except Exception:  # noqa: BLE001 — unreadable git: nothing verifiable
        return []
    if result.returncode != 0:
        return []
    bump_map = _git_log_bump_map(result.stdout)
    mainline_history_truncated = _git_history_is_truncated(root)

    # Collect records and the SHAs they cite, then resolve all SHAs in one batch.
    records: list[tuple[str, str, int, int, str]] = []
    shas: set[str] = set()
    ledger_history = ledger.get("history") if isinstance(ledger, dict) else None
    for section, keys in (ledger_history or {}).items():
        if not isinstance(keys, dict):
            continue
        for key, recs in keys.items():
            if not isinstance(recs, list):
                continue
            for rec in recs:
                if not isinstance(rec, dict):
                    continue
                sha = str(rec.get("commit_sha") or "")
                try:
                    old_cap = int(rec["old_cap"])
                    new_cap = int(rec["new_cap"])
                except (KeyError, TypeError, ValueError):
                    continue
                if sha and sha != UNATTRIBUTED_COMMIT_SHA:
                    shas.add(sha)
                records.append((section, key, old_cap, new_cap, sha))
    resolved = _resolve_shas(shas, root) if shas else {}

    # Records whose commit is NOT in the (possibly partial) mainline log still
    # have to be adjudicated one at a time.  On a truncated checkout — every CI
    # shard and agent VM, where the log reaches only the shallow boundary —
    # that is nearly EVERY record, and the per-record form forks git four times
    # apiece: the ancestry probe plus its nested ref lookup, the shallow-file
    # resolve, the parent count, and the diff render.  At the current ledger
    # size that is thousands of forks inside one guard call, which is what
    # pushed this check past CI's 30s pytest-timeout (run 32805982004, backend
    # shard 8, inside _commit_in_mainline_ancestry).  Answer all of them up
    # front in three forks instead.  Each batch falls back to its per-SHA
    # helper when git cannot read it, so no verdict — and no polarity — moves.
    beyond_log_shas = sorted(
        {
            sha
            for _section, _key, _old_cap, _new_cap, sha in records
            if sha
            and sha != UNATTRIBUTED_COMMIT_SHA
            and sha not in bump_map
            and resolved.get(sha, False)
        }
    )
    beyond_log_set = frozenset(beyond_log_shas)
    ancestor_shas = _mainline_ancestor_shas(ref, root) if beyond_log_shas else None
    boundary_shas = _shallow_boundary_shas(root)
    parent_counts = _batch_commit_parent_counts(beyond_log_shas, root)
    batched_diffs = _batch_commit_file_diffs(beyond_log_shas, root)

    phantoms: list[tuple[str, str, int, int, str, str]] = []
    for section, key, old_cap, new_cap, sha in records:
        if not sha or sha == UNATTRIBUTED_COMMIT_SHA:
            # Rollup-pending or legacy unattributed bump: written before the
            # commit that bumps the cap exists; nothing to verify against yet.
            continue
        pair = (old_cap, new_cap)
        logged = bump_map.get(sha)
        if logged is not None:
            # The commit is in the (partial) mainline log — we have its diff.
            if key in logged.unattributed_keys:
                # The diff touches this key but its enclosing section could not
                # be read, so it cannot corroborate OR contradict a record
                # filed under a particular section. Absent evidence, not proof.
                continue
            if pair not in logged.by_section_key.get((section, key), set()):
                phantoms.append(
                    (
                        section,
                        key,
                        old_cap,
                        new_cap,
                        sha,
                        "commit's diff shows no recorded bump for this key/pair",
                    )
                )
            continue
        # SHA not in the mainline log: resolve it and pin down why.
        if not resolved.get(sha, False):
            # Evidence is absent, not positive proof of fabrication.
            continue
        ancestry = _ancestry_verdict(sha, ancestor_shas, root)
        if ancestry is False:
            if not mainline_history_truncated:
                phantoms.append(
                    (
                        section,
                        key,
                        old_cap,
                        new_cap,
                        sha,
                        "commit is not in the mainline ancestry",
                    )
                )
                continue
            # ``merge-base --is-ancestor`` also exits 1 when the connecting
            # path lies beyond a shallow boundary. A reference clone can still
            # resolve the older commit object, so suppress only the ambiguous
            # ancestry verdict and continue to inspect its direct diff below.
        if ancestry is None:
            # An ancestry probe failure is evidence loss, not proof of a fake
            # record. Never delete on this polarity.
            continue
        if sha in boundary_shas:
            # The parent is deliberately hidden, so ``git show`` synthesizes a
            # root/full-file-add diff. A missing pair there is evidence loss,
            # not proof that the ledger row is fabricated.
            continue
        parent_count = (
            parent_counts[sha]
            if parent_counts is not None and sha in parent_counts
            else _git_commit_parent_count(sha, root)
        )
        if parent_count is None or parent_count > 1:
            # A merge commit has no single-parent diff that can attribute a
            # cap pair. Likewise, a failed parent probe leaves the provenance
            # absent. Neither case is positive evidence of a phantom.
            continue
        # A beyond-boundary ancestor whose objects are present in this (possibly
        # shallow) checkout: render its diff directly and verify the pair.
        diff_text = (
            batched_diffs.get(sha, "")
            if batched_diffs is not None and sha in beyond_log_set
            else _git_show_file_diff(sha, root)
        )
        if diff_text is None:
            # An unreadable diff cannot establish fabrication.
            continue
        if _is_combined_git_diff(diff_text):
            # A merge commit is readable, but its combined diff does not expose
            # the single-parent pair format required for attribution.
            continue
        shown = _scan_diff_bumps(diff_text)
        if key in shown.unattributed_keys:
            # Section-unattributable evidence: absent, not contradictory.
            continue
        shown_pairs = shown.by_section_key.get((section, key), set())
        if pair not in shown_pairs:
            phantoms.append(
                (
                    section,
                    key,
                    old_cap,
                    new_cap,
                    sha,
                    "commit's diff shows no recorded bump for this key/pair",
                )
            )
    return phantoms


def cap_history_chain_breaks(history: list[BumpRecord]) -> list[tuple[int, int]]:
    """Return upward breaks in a module's ordered cap-bump history.

    Consecutive records should form a chain: the next record's ``old_cap``
    equals the previous record's ``new_cap``. A lower next ``old_cap`` is a
    legitimate cap decrease and is intentionally ignored; only an upward gap
    can represent a deleted cap-raise record. The returned pairs are
    ``(previous_new_cap, next_old_cap)``.
    """
    ordered = sorted(history, key=lambda r: _parse_iso(r["commit_date"]))
    return [
        (previous["new_cap"], current["old_cap"])
        for previous, current in zip(ordered, ordered[1:])
        if current["old_cap"] > previous["new_cap"]
    ]


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
    # A missing record is evidence loss, not permission to grant a routine
    # bump. An upward chain break is the observable residue of a deleted cap
    # raise; classify it as chronic so self-heal cannot loosen governance while
    # the ledger is incomplete. Downward breaks are legal cap decreases and are
    # deliberately excluded by cap_history_chain_breaks().
    if cap_history_chain_breaks(history):
        return True

    if len(history) < CHRONIC_BUMP_THRESHOLD:
        return False

    ordered = sorted(history, key=lambda r: _parse_iso(r["commit_date"]))

    # Trigger 1 — window check.
    cutoff = datetime.now(timezone.utc) - timedelta(days=CHRONIC_WINDOW_DAYS)
    recent_count = sum(1 for r in ordered if _parse_iso(r["commit_date"]) >= cutoff)
    if recent_count >= CHRONIC_BUMP_THRESHOLD:
        return True

    # Trigger 2 — cumulative growth check.
    first_cap = ordered[0]["old_cap"]
    current_cap = ordered[-1]["new_cap"]
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
    repo_root: str | None = None,
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
        history = parse_bump_history(module_key, section, repo_root)
    except BumpHistoryUnavailableError as exc:
        # Unreadable history is NOT "no bumps on record": refuse the automatic
        # bump and route to decomposition. An explicit operator override
        # (override_chronic=True) still grants the bump — it is the documented
        # human-approval escape hatch, identical to a verified chronic offender.
        if override_chronic:
            try:
                suggested_new_limit = bump_size_limit(
                    current_size, module_key, section, repo_root
                )
            except GovernanceCapExceededError as exc:
                return BreachVerdict(
                    is_chronic=True,
                    suggested_new_limit=None,
                    bump_count=0,
                    history_unavailable=True,
                    reason=f"Refusing cap bump: {exc}.",
                )
            return BreachVerdict(
                is_chronic=True,
                suggested_new_limit=suggested_new_limit,
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

    try:
        suggested_new_limit = bump_size_limit(
            current_size, module_key, section, repo_root
        )
    except GovernanceCapExceededError as exc:
        return BreachVerdict(
            is_chronic=True,
            suggested_new_limit=None,
            bump_count=bump_count,
            history_unavailable=False,
            reason=f"Refusing cap bump: {exc}.",
        )

    return BreachVerdict(
        is_chronic=chronic,
        suggested_new_limit=suggested_new_limit,
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
