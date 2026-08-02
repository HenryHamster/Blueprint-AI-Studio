"""Failure-layer classifier — pure-function fingerprint → layer-tag mapping.

This is the slim survivor of the removed pipeline-regression apparatus's
``regression_layer_classifier``. Only the pure, side-effect-free classification
helpers that live consumers still use are kept:

  * ``classify_layers`` — map a (failure_class, message, stage) fingerprint to a
    subset of the six canonical layer tags. Used by ``sr_blind_spots`` to build
    per-theme layer-distribution analytics on live self-review blind-spot data.
  * ``parse_viewport_regression_class`` — parse a ``viewport_regression:`` class
    string into component / assertion-type / width.
  * ``ALL_LAYERS`` and the ``LAYER_*`` constants.

The dispatcher-only aggregation/meta-prompt helpers (build_layer_meta_block,
aggregate_targeted_layers, is_recurring_cross_layer_class,
compute_layer_defense_counts, compute_untargeted_layers,
compute_recovery_layer_mismatch, LAYER_DESCRIPTIONS, LAYER_DISPLAY_ORDER) were
dropped with the apparatus — they had no surviving consumer.

Pure-function module — no DB, no session, no side effects.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical layer tags
# ---------------------------------------------------------------------------

LAYER_CI_WORKFLOW = "CI_WORKFLOW"
LAYER_AGENT_VM = "AGENT_VM"
LAYER_AGENT_VERB = "AGENT_VERB"
LAYER_BASELINE_GATE = "BASELINE_GATE"
LAYER_REVIEWER_PASS = "REVIEWER_PASS"
LAYER_VIEWPORT = "VIEWPORT"

ALL_LAYERS: frozenset[str] = frozenset(
    [
        LAYER_CI_WORKFLOW,
        LAYER_AGENT_VM,
        LAYER_AGENT_VERB,
        LAYER_BASELINE_GATE,
        LAYER_REVIEWER_PASS,
        LAYER_VIEWPORT,
    ]
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# AGENT_VM: system-library / package install / runtime missing
_AGENT_VM_PATTERNS: list[re.Pattern] = [
    re.compile(r"error while loading shared librar", re.IGNORECASE),
    re.compile(r"libatk[-\w]*\.so", re.IGNORECASE),
    re.compile(r"no such file or directory.*\.so", re.IGNORECASE),
    re.compile(r"cannot open shared object file", re.IGNORECASE),
    re.compile(r"playwright.*system.*dep", re.IGNORECASE),
    re.compile(r"apt-get install", re.IGNORECASE),
    re.compile(r"dpkg.*not installed", re.IGNORECASE),
    re.compile(r"missing.*system.*librar", re.IGNORECASE),
    re.compile(r"prefetch.*asset.*fail", re.IGNORECASE),
    re.compile(r"packer.*build.*fail", re.IGNORECASE),
    # Node/Python runtime not found on VM
    re.compile(r"node: command not found", re.IGNORECASE),
    re.compile(r"python3?: command not found", re.IGNORECASE),
    re.compile(r"sudo.*sudoers", re.IGNORECASE),
    # dash/sh missing-executable phrasing ("<tool>: not found"), anchored to the
    # shell prefix so it cannot match a repo dependency or assertion text.
    re.compile(
        r"(?:/(?:usr/)?bin/)?(?:sh|bash|dash|ash):\s*(?:\d+:\s*)?\S+:\s+not found",
        re.IGNORECASE,
    ),
    # Docker daemon unavailable on the gate VM
    re.compile(r"cannot connect to the docker daemon", re.IGNORECASE),
    re.compile(r"is the docker daemon running", re.IGNORECASE),
    re.compile(r"docker daemon is not running", re.IGNORECASE),
    # Out-of-memory kills (exit 137 / SIGKILL / signal 9)
    re.compile(r"signal:\s*killed", re.IGNORECASE),
    re.compile(r"sigkill", re.IGNORECASE),
    re.compile(r"signal 9", re.IGNORECASE),
    re.compile(r"(?:exit(?:ed with)?|exit code|exit status|code|status)\s+137", re.IGNORECASE),
    re.compile(r"oomkilled", re.IGNORECASE),
    re.compile(r"out of memory:\s*kill", re.IGNORECASE),
    re.compile(r"javascript heap out of memory", re.IGNORECASE),
    re.compile(r"java\.lang\.outofmemoryerror", re.IGNORECASE),
    re.compile(r"cannot allocate memory", re.IGNORECASE),
    # Disk/quota/space failures (host resource exhaustion on the gate VM)
    re.compile(r"no space left on device", re.IGNORECASE),
    re.compile(r"disk quota exceeded", re.IGNORECASE),
    re.compile(r"filesystem (is )?full", re.IGNORECASE),
    re.compile(r"enospc", re.IGNORECASE),
    # Permission denied / access failures (read-only fs, insufficient perms)
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"eacces", re.IGNORECASE),
    re.compile(r"read-only file system", re.IGNORECASE),
    re.compile(r"erofs", re.IGNORECASE),
]

_AGENT_VM_FAILURE_CLASSES: frozenset[str] = frozenset(
    ["PROVISION_FAILURE", "BLOCKED_PRECONDITION"]
)

_AGENT_VM_STAGE_KEYWORDS: list[str] = ["provision", "packer", "vm_init", "agent_boot"]

# CI_WORKFLOW: workflow path-filter misses, no-jobs-ran, trigger misses
_CI_WORKFLOW_PATTERNS: list[re.Pattern] = [
    re.compile(r"no jobs? (ran|were triggered|matched)", re.IGNORECASE),
    re.compile(r"workflow.*not triggered", re.IGNORECASE),
    re.compile(r"path.?filter.*miss", re.IGNORECASE),
    re.compile(r"changed file.*not in.*trigger", re.IGNORECASE),
    re.compile(r"\.github/workflows", re.IGNORECASE),
    re.compile(r"workflow.*skip", re.IGNORECASE),
    re.compile(r"on\.push\.paths.*did not match", re.IGNORECASE),
    re.compile(r"ci.*not run", re.IGNORECASE),
    re.compile(r"github actions.*fail", re.IGNORECASE),
]

_CI_WORKFLOW_FAILURE_CLASSES: frozenset[str] = frozenset(
    ["BLOCKED_PRECONDITION", "EXTERNAL_API"]
)

_CI_WORKFLOW_STAGE_KEYWORDS: list[str] = ["ci", "workflow", "github_actions", "trigger"]

# AGENT_VERB: scope-too-broad, missing ./check coverage, handler invocation issues
_AGENT_VERB_PATTERNS: list[re.Pattern] = [
    re.compile(r"scope.{0,20}too.{0,10}broad", re.IGNORECASE),
    re.compile(r"outside.{0,30}declared.{0,20}scope", re.IGNORECASE),
    re.compile(r"agent.*invoked.*outside.*scope", re.IGNORECASE),
    re.compile(r"missing.*\.\/check.*coverage", re.IGNORECASE),
    re.compile(r"handler.*not.{0,10}(declared|registered)", re.IGNORECASE),
    re.compile(r"verb.*not.{0,10}allowed", re.IGNORECASE),
    re.compile(r"check.*gate.*not.*configured", re.IGNORECASE),
    re.compile(r"task.type.*not.*handled", re.IGNORECASE),
]

_AGENT_VERB_STAGE_KEYWORDS: list[str] = [
    "handler",
    "check_gate",
    "agent_verb",
    "scope_check",
]

# BASELINE_GATE: lint/typecheck failures in unchanged files, scope-ratchet violations
_BASELINE_GATE_PATTERNS: list[re.Pattern] = [
    # ruff/mypy/eslint errors in unchanged files — the key signal
    re.compile(r"(ruff|mypy|eslint|flake8|pyright).*error.*unchanged", re.IGNORECASE),
    re.compile(r"errors? in unchanged file", re.IGNORECASE),
    re.compile(r"lint.*fail.*unrelated", re.IGNORECASE),
    re.compile(r"typecheck.*fail.*unrelated", re.IGNORECASE),
    re.compile(r"baseline.*violation", re.IGNORECASE),
    re.compile(r"scope.*ratchet.*fail", re.IGNORECASE),
    re.compile(r"pre.?existing.{0,20}(lint|type).?error", re.IGNORECASE),
    # Broad lint failures that cascade beyond the changed files
    re.compile(r"(ruff|mypy|eslint).*E\d{3,}", re.IGNORECASE),
    re.compile(r"module.size.limit", re.IGNORECASE),
    # Viewport regression gate — viewport.test.tsx failures and coverage drops
    re.compile(r"viewport.*test.*\.tsx", re.IGNORECASE),
    re.compile(r"expect(?:NoOverflow|MinTouchTarget)At", re.IGNORECASE),
    re.compile(
        r"viewport.{0,30}(?:overflow|regression|baseline|coverage)", re.IGNORECASE
    ),
    re.compile(r"check-viewport\.sh", re.IGNORECASE),
]

_BASELINE_GATE_FAILURE_CLASSES: frozenset[str] = frozenset(
    ["CODE_ERROR", "BLOCKED_PRECONDITION"]
)

_BASELINE_GATE_STAGE_KEYWORDS: list[str] = [
    "lint",
    "typecheck",
    "baseline",
    "eslint",
    "ruff",
    "mypy",
    "viewport",
]

# REVIEWER_PASS: independent-review disagreement and build-gate conflicts
_REVIEWER_PASS_PATTERNS: list[re.Pattern] = [
    re.compile(r"reviewer.{0,30}disagree", re.IGNORECASE),
    re.compile(r"build.{0,10}gate.{0,20}(disagree|conflict)", re.IGNORECASE),
    re.compile(r"reviewer.pass.{0,20}(fail|reject)", re.IGNORECASE),
    re.compile(r"review.{0,10}pipeline.{0,20}regression", re.IGNORECASE),
    re.compile(r"reviewer.{0,20}recovery", re.IGNORECASE),
    re.compile(r"second.{0,10}opinion.{0,20}(fail|differ)", re.IGNORECASE),
    re.compile(r"review.*catch.{0,20}(miss|escape)", re.IGNORECASE),
]

_REVIEWER_PASS_STAGE_KEYWORDS: list[str] = [
    "review",
    "reviewer",
    "review_pipeline",
]

# VIEWPORT: client-rendering / 375px viewport regressions in admin-ui components
_VIEWPORT_PATTERNS: list[re.Pattern] = [
    re.compile(r"viewport.{0,20}regression", re.IGNORECASE),
    re.compile(r"expectNoOverflowAt", re.IGNORECASE),
    re.compile(r"expectMinTouchTargetAt", re.IGNORECASE),
    re.compile(r"expectNoTextTruncationAt", re.IGNORECASE),
    re.compile(r"\.viewport\.test\.tsx", re.IGNORECASE),
    re.compile(r"375px", re.IGNORECASE),
    re.compile(r"mobile.{0,20}width.{0,20}overflow", re.IGNORECASE),
]

# Both viewport failure classes belong to the VIEWPORT defense layer: rendering
# regressions (VIEWPORT_REGRESSION) and coverage erosion (VIEWPORT_COVERAGE_DROP).
# Values are upper-case to match the ``(failure_class or "").upper()`` normalization
# at the call site in classify_layers (VIEWPORT_COVERAGE_DROP's canonical constant
# in failure_classification.py is lowercase "viewport_coverage_drop"). Without
# this, a coverage-drop failure class never tagged LAYER_VIEWPORT, leaving the
# INFRA/relevant-class checks inconsistent with its sibling VIEWPORT_REGRESSION.
_VIEWPORT_FAILURE_CLASSES: frozenset[str] = frozenset(
    ["VIEWPORT_REGRESSION", "VIEWPORT_COVERAGE_DROP"]
)

_VIEWPORT_STAGE_KEYWORDS: list[str] = ["viewport"]

# ---------------------------------------------------------------------------
# Layer descriptions (used by meta-investigation prompt builders)
# ---------------------------------------------------------------------------


def classify_layers(
    failure_class: str,
    message: str,
    stage: str | None = None,
) -> list[str]:
    """Return layer tags that match the given failure fingerprint.

    Parameters
    ----------
    failure_class:
        String classification of the failure (e.g. ``"CODE_ERROR"``,
        ``"PROVISION_FAILURE"``). Must not be empty.
    message:
        Free-text error/log message to fingerprint against.
    stage:
        Optional pipeline stage name (e.g. ``"provision"``, ``"lint"``).

    Returns
    -------
    list[str]
        Subset of ``ALL_LAYERS``. Empty list if no layer matched — callers
        must NOT default-bucket unclassifiable inputs.
    """
    tags: list[str] = []

    _stage = (stage or "").lower()
    _fc = (failure_class or "").upper()

    # --- AGENT_VM ---
    if _matches_any(message, _AGENT_VM_PATTERNS) or any(
        kw in _stage for kw in _AGENT_VM_STAGE_KEYWORDS
    ):
        tags.append(LAYER_AGENT_VM)
    elif _fc in _AGENT_VM_FAILURE_CLASSES and _stage in (
        "provision",
        "vm_init",
        "agent_boot",
    ):
        tags.append(LAYER_AGENT_VM)

    # --- CI_WORKFLOW ---
    if _matches_any(message, _CI_WORKFLOW_PATTERNS) or any(
        kw in _stage for kw in _CI_WORKFLOW_STAGE_KEYWORDS
    ):
        tags.append(LAYER_CI_WORKFLOW)

    # --- AGENT_VERB ---
    if _matches_any(message, _AGENT_VERB_PATTERNS) or any(
        kw in _stage for kw in _AGENT_VERB_STAGE_KEYWORDS
    ):
        tags.append(LAYER_AGENT_VERB)

    # --- VIEWPORT ---
    # Checked before BASELINE_GATE so viewport inputs are never also tagged BASELINE_GATE.
    if (
        _fc in _VIEWPORT_FAILURE_CLASSES
        or _matches_any(message, _VIEWPORT_PATTERNS)
        or any(kw in _stage for kw in _VIEWPORT_STAGE_KEYWORDS)
    ):
        tags.append(LAYER_VIEWPORT)

    # --- BASELINE_GATE --- (skipped when VIEWPORT already matched)
    if LAYER_VIEWPORT not in tags:
        if _matches_any(message, _BASELINE_GATE_PATTERNS) or any(
            kw in _stage for kw in _BASELINE_GATE_STAGE_KEYWORDS
        ):
            tags.append(LAYER_BASELINE_GATE)
        elif _fc in _BASELINE_GATE_FAILURE_CLASSES and any(
            kw in _stage for kw in ("lint", "typecheck", "ruff", "mypy", "eslint")
        ):
            tags.append(LAYER_BASELINE_GATE)

    # --- REVIEWER_PASS ---
    if _matches_any(message, _REVIEWER_PASS_PATTERNS) or any(
        kw in _stage for kw in _REVIEWER_PASS_STAGE_KEYWORDS
    ):
        tags.append(LAYER_REVIEWER_PASS)

    return tags


def parse_viewport_regression_class(regression_class: str) -> dict[str, str | None]:
    """Parse a viewport_regression: class string into component, assertion type, and width.

    Handles both formats:
    - Old: ``viewport_regression:<Component>@<width>`` (assertion_type → None)
    - New: ``viewport_regression:<Component>:<assertion_type>@<width>``

    Returns a dict with keys ``component``, ``assertion_type``, and ``width``.
    All values are ``None`` for strings that don't start with ``viewport_regression:``.
    """
    _empty: dict[str, str | None] = {
        "component": None,
        "assertion_type": None,
        "width": None,
    }
    prefix = "viewport_regression:"
    if not regression_class.startswith(prefix):
        return _empty
    inner = regression_class[len(prefix) :]
    at_parts = inner.split("@", 1)
    if len(at_parts) != 2:
        return _empty
    left, width = at_parts
    colon_parts = left.split(":", 1)
    assertion_type: str | None
    if len(colon_parts) == 2:
        component, assertion_type = colon_parts
    else:
        component, assertion_type = left, None
    return {"component": component, "assertion_type": assertion_type, "width": width}


def _matches_any(text: str, patterns: list[re.Pattern]) -> bool:
    """Return True if any pattern matches the given text."""
    for pat in patterns:
        if pat.search(text):
            return True
    return False
