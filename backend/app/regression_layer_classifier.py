"""Layer classifier for pipeline regression symptoms.

Pure-function module — no DB, no session, no side effects.
Returns a subset of the six canonical layer tags based on fingerprint
heuristics applied to failure_class, message text, and optional stage name.

Layer tags:
  CI_WORKFLOW   — .github/workflows/*.yml (triggers, path filters, jobs)
  AGENT_VM      — infra/packer/agent.pkr.hcl (system libs, runtimes, sudoers)
  AGENT_VERB    — agent/src/handlers/ (what gates fire, scope enforcement)
  BASELINE_GATE — lint/test/typecheck baselines, scope rules, strictness
  REVIEWER_PASS — fresh-reviewer pass, build-gate disagreement detection
  VIEWPORT      — client-rendering / 375px viewport regressions in admin-ui components
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
]

# REVIEWER_PASS: fresh-reviewer disagreement, build-gate conflicts
_REVIEWER_PASS_PATTERNS: list[re.Pattern] = [
    re.compile(r"reviewer.{0,30}disagree", re.IGNORECASE),
    re.compile(r"fresh.{0,10}reviewer.{0,30}(fail|reject|flag)", re.IGNORECASE),
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
    "fresh_reviewer",
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

_VIEWPORT_FAILURE_CLASSES: frozenset[str] = frozenset(["VIEWPORT_REGRESSION"])

_VIEWPORT_STAGE_KEYWORDS: list[str] = ["viewport"]

# ---------------------------------------------------------------------------
# Layer descriptions (used by meta-investigation prompt builders)
# ---------------------------------------------------------------------------

LAYER_DESCRIPTIONS: dict[str, str] = {
    LAYER_CI_WORKFLOW: ".github/workflows/*.yml — triggers, path filters, job definitions",
    LAYER_AGENT_VM: "infra/packer/agent.pkr.hcl — system libs, runtimes, sudoers",
    LAYER_AGENT_VERB: "agent/src/handlers/ — gate scope enforcement, handler invocations",
    LAYER_BASELINE_GATE: "lint/typecheck baselines, scope rules, module size limits",
    LAYER_REVIEWER_PASS: "fresh-reviewer pass, build-gate disagreement detection",
    LAYER_VIEWPORT: "client-rendering / 375px viewport regressions in admin-ui components",
}

# Stable display order for meta-investigation prompts.
_LAYER_DISPLAY_ORDER: tuple[str, ...] = (
    LAYER_CI_WORKFLOW,
    LAYER_AGENT_VM,
    LAYER_AGENT_VERB,
    LAYER_BASELINE_GATE,
    LAYER_REVIEWER_PASS,
    LAYER_VIEWPORT,
)


def build_layer_meta_block(
    targeted_layers: frozenset[str] | set[str] | None = None,
) -> str:
    """Return a formatted layer-catalog block for meta-investigation prompts.

    Lists every canonical layer with its description and marks each as
    'targeted' (already defended) or 'UNTARGETED' (candidate for next defense).
    Callers should embed this block in the investigation prompt so investigators
    know which layers to prioritise.
    """
    targeted = frozenset(targeted_layers or [])
    lines: list[str] = ["Canonical defense layers:"]
    for layer in _LAYER_DISPLAY_ORDER:
        status = "targeted" if layer in targeted else "UNTARGETED"
        desc = LAYER_DESCRIPTIONS.get(layer, "")
        lines.append(f"  {layer} [{status}]: {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
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


def compute_untargeted_layers(fix_log: list[dict]) -> list[str]:
    """Return canonical layer names that have zero targeted fixes in fix_log.

    Scans each entry's ``layers_targeted`` field (a list of layer name strings).
    Entries missing the field, or with an empty list, contribute nothing to the
    "targeted" set — they are treated as unclassified, not as targeting all layers.
    Unknown layer names are silently ignored.

    Returns a sorted list of layer names from ``ALL_LAYERS`` that appear in no
    fix entry's ``layers_targeted``.  Returns all five layers when fix_log is
    empty or no entry carries a ``layers_targeted`` field.
    """
    targeted: set[str] = set()
    for entry in fix_log:
        for layer in entry.get("layers_targeted") or []:
            if layer in ALL_LAYERS:
                targeted.add(layer)
    return sorted(ALL_LAYERS - targeted)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matches_any(text: str, patterns: list[re.Pattern]) -> bool:
    """Return True if any pattern matches the given text."""
    for pat in patterns:
        if pat.search(text):
            return True
    return False
