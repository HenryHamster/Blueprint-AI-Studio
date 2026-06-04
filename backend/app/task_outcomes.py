"""Per-task-type terminal outcome primitive.

Blueprint's strategic analytics historically defined task success in exactly
one way: a merged PR (``landed_signal.is_merged_completion``). That is correct
for *write* task types (create / fix / refactor / migrate / …) which push a
branch and open a PR, but the *read-only* task types (research, the audit
family, memo, verify_tests, …) never open a PR. They each emit an honest
terminal signal of their own — research actionability, structured audit
findings, a memo reasoning artifact, a verify-tests merge-gate verdict — and
under the merged-PR-only definition every one of those signals is invisible to
success metrics.

This module is a small, dependency-light leaf that, given a terminal
:class:`~app.models.Task`, returns its evaluation :class:`TaskOutcome`
*independent of PR status*: a ``status`` (``success`` | ``neutral`` |
``failure``) plus an ``kind`` label naming *how* the task was judged
(``code_merge``, ``research_quality``, ``audit_findings``, ``memo_artifact``,
``verification_verdict``, …).

Design notes
------------
* Per-type semantics are resolved through the canonical task-type registry
  (:mod:`app.task_capabilities`) — the write-vs-read-only split keys off
  ``WRITE_TASK_TYPES`` / ``agent_mode``, NOT a hardwired ``if/elif`` on type
  names. New write task types are honored automatically; unknown / future
  types degrade to ``neutral`` so the primitive is forward-compatible.
* Write-type semantics are preserved *exactly*: ``success`` iff
  ``is_merged_completion(task)``, ``failure`` iff ``_counts_as_failure(task)``
  (the existing direction-metrics failure notion), else ``neutral``. We wrap
  those predicates rather than re-deriving them, so ``landed_signal`` behavior
  is unchanged.
* A registry seam, :data:`OUTCOME_EVALUATORS`, is consulted *before* the
  built-in defaults so the in-flight evaluator framework (``task_evaluators``)
  can register a per-type evaluator-key/verdict override without editing this
  module's default logic.

Nothing imports this module yet — it is a pure leaf. The strategic analytics
consumers (digest metrics, strategic analysis, direction outcomes) adopt it in
follow-on tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from app.ci_resolution import classify_merge_test_verdict
from app.direction_outcomes import _counts_as_failure
from app.landed_signal import is_merged_completion
from app.models import Task
from app.task_capabilities import WRITE_TASK_TYPES, get_task_capability

OutcomeStatus = Literal["success", "neutral", "failure"]


@dataclass(frozen=True)
class TaskOutcome:
    """A task's terminal evaluation, independent of PR status.

    ``status`` is the three-way success classification; ``kind`` names the
    signal the classification was derived from so analytics can bucket
    outcomes by *how* they were judged (e.g. group all ``research_quality``
    successes separately from ``code_merge`` ones).
    """

    status: OutcomeStatus
    kind: str


# Outcome-kind labels. Centralised so callers bucket on a stable vocabulary.
KIND_CODE_MERGE = "code_merge"
KIND_RESEARCH_QUALITY = "research_quality"
KIND_AUDIT_FINDINGS = "audit_findings"
KIND_MEMO_ARTIFACT = "memo_artifact"
KIND_VERIFICATION_VERDICT = "verification_verdict"
# Read-only types that have no type-specific honest signal yet (review, plan,
# strategic_review, design_doc_update, smoketest, debug, investigate). They are
# terminal but contribute no success/failure signal of their own — neutral.
KIND_READ_ONLY_UNCLASSIFIED = "read_only_unclassified"
# Unknown / future task type with no registry entry.
KIND_UNKNOWN = "unknown"


def _task_local_attr(task: Task, attr: str, default: Any = None) -> Any:
    """Read an ORM-backed task attribute without triggering a lazy load.

    Digest/admin callers often load ``Task`` rows with ``load_only()`` to keep
    large scans bounded. Accessing a deferred column on an async ORM instance in
    a synchronous helper can raise ``MissingGreenlet``. Outcome evaluation is a
    best-effort primitive, so when a column was not selected we degrade to the
    provided default instead of issuing implicit IO.
    """
    projected_name = f"_projected_{attr}"
    if hasattr(task, projected_name):
        return getattr(task, projected_name)
    return vars(task).get(attr, default)


def _task_type(task: Task) -> str | None:
    """Resolve a task's declared type from its first payload, or None."""
    return (task.first_payload or {}).get("type")


# ---------------------------------------------------------------------------
# Write / PR task types — preserve current merged-PR semantics exactly.
# ---------------------------------------------------------------------------


def _evaluate_write(task: Task) -> TaskOutcome:
    """Outcome for a write/PR task type.

    ``success`` iff the task is a merged completion, ``failure`` iff it counts
    as a genuine failure for direction metrics, else ``neutral`` (e.g. a
    COMPLETED task that opened no PR, or a BLOCKED holding state).
    """
    if is_merged_completion(task):
        return TaskOutcome("success", KIND_CODE_MERGE)
    if _counts_as_failure(task):
        return TaskOutcome("failure", KIND_CODE_MERGE)
    return TaskOutcome("neutral", KIND_CODE_MERGE)


# ---------------------------------------------------------------------------
# Read-only task types — derive success from each type's own terminal output.
# ---------------------------------------------------------------------------


def _findings_list(plan_output: Any) -> list[dict]:
    """Extract a list of structured finding dicts from a task's plan_output.

    Tolerates both shapes handlers populate: a bare list of findings (research,
    self_audit) and the ``{"findings": [...]}`` wrapper (security_audit, perf).
    """
    if isinstance(plan_output, list):
        return [f for f in plan_output if isinstance(f, dict)]
    if isinstance(plan_output, dict):
        findings = plan_output.get("findings")
        if isinstance(findings, list):
            return [f for f in findings if isinstance(f, dict)]
    return []


def _evaluate_research(task: Task) -> TaskOutcome:
    """Outcome for a research task — judged on actionable findings produced.

    A research task stores its findings as a bare list on ``plan_output``, each
    a dict carrying a ``title`` and an ``actionability_grade``. Producing at
    least one titled finding is the honest success signal; producing none is a
    terminal-but-empty ``neutral`` (no usable output). A FAILED/CANCELLED
    research task is a ``failure``.
    """
    if _counts_as_failure(task):
        return TaskOutcome("failure", KIND_RESEARCH_QUALITY)
    plan_output = _task_local_attr(task, "plan_output")
    actionable = [f for f in _findings_list(plan_output) if f.get("title")]
    if actionable:
        return TaskOutcome("success", KIND_RESEARCH_QUALITY)
    return TaskOutcome("neutral", KIND_RESEARCH_QUALITY)


def _evaluate_audit(task: Task) -> TaskOutcome:
    """Outcome for an audit/perf task — judged on structured findings produced.

    security_audit / perf / self_audit / release_audit all emit structured
    findings (severity- or maturity-scored). Producing any structured finding
    is the honest success signal — the audit did its job — while an empty
    result is ``neutral``. A FAILED/CANCELLED audit is a ``failure``.
    """
    if _counts_as_failure(task):
        return TaskOutcome("failure", KIND_AUDIT_FINDINGS)
    if _findings_list(_task_local_attr(task, "plan_output")):
        return TaskOutcome("success", KIND_AUDIT_FINDINGS)
    return TaskOutcome("neutral", KIND_AUDIT_FINDINGS)


def _evaluate_memo(task: Task) -> TaskOutcome:
    """Outcome for a memo task — judged on the reasoning artifact's presence.

    The memo handler mirrors the normalized memo into ``plan_output['memo']``
    when it persists the MEMO artifact, so a present, non-empty ``memo`` is the
    honest success signal without a DB round-trip. A memo task that completed
    with no parseable reasoning is ``neutral``; a FAILED/CANCELLED one is a
    ``failure``.
    """
    if _counts_as_failure(task):
        return TaskOutcome("failure", KIND_MEMO_ARTIFACT)
    raw_plan_output = _task_local_attr(task, "plan_output")
    plan_output = raw_plan_output if isinstance(raw_plan_output, dict) else {}
    if plan_output.get("memo"):
        return TaskOutcome("success", KIND_MEMO_ARTIFACT)
    return TaskOutcome("neutral", KIND_MEMO_ARTIFACT)


def _verify_tests_verdict(task: Task) -> dict | None:
    """Best-effort read of a verify_tests task's merge-gate verdict dict.

    The canonical verdict home is the MERGE_TEST_RESULT artifact (read by the
    orchestrator's no-CI gate), which a DB-free primitive cannot load. We
    instead read whatever task-local mirror is present: a verdict-shaped
    ``plan_output`` (carrying ``detected`` / ``passed``) or a verdict stashed on
    ``context``. This keeps the primitive forward-compatible — when the
    evaluator framework mirrors the verdict onto the task, this picks it up —
    and degrades to ``None`` (→ ``undetected`` → ``neutral``) when absent.
    """
    plan_output = _task_local_attr(task, "plan_output")
    if isinstance(plan_output, dict) and (
        "detected" in plan_output or "passed" in plan_output
    ):
        return plan_output
    ctx = _task_local_attr(task, "context") or {}
    verdict = ctx.get("merge_test_verdict") or ctx.get("verify_tests_verdict")
    if isinstance(verdict, dict):
        return verdict
    return None


def _evaluate_verify_tests(task: Task) -> TaskOutcome:
    """Outcome for a verify_tests task — judged on its merge-gate verdict.

    Reuses :func:`classify_merge_test_verdict` so the pass/fail/undetected
    mapping stays identical to the no-CI merge gate: a ``pass`` verdict is a
    ``success``, a ``fail`` verdict is a ``failure``, and ``undetected`` (no
    runnable suite, or no usable verdict) is ``neutral``. A FAILED/CANCELLED
    verify task is a ``failure``.
    """
    if _counts_as_failure(task):
        return TaskOutcome("failure", KIND_VERIFICATION_VERDICT)
    outcome, _detail = classify_merge_test_verdict(_verify_tests_verdict(task))
    if outcome == "pass":
        return TaskOutcome("success", KIND_VERIFICATION_VERDICT)
    if outcome == "fail":
        return TaskOutcome("failure", KIND_VERIFICATION_VERDICT)
    return TaskOutcome("neutral", KIND_VERIFICATION_VERDICT)


# Built-in per-type evaluators for read-only task types that carry an honest
# terminal signal. Keyed by task-type value (the registry's canonical id). Read
# only types absent from this map degrade to KIND_READ_ONLY_UNCLASSIFIED.
_READ_ONLY_EVALUATORS: dict[str, Callable[[Task], TaskOutcome]] = {
    "research": _evaluate_research,
    "security_audit": _evaluate_audit,
    "perf": _evaluate_audit,
    "self_audit": _evaluate_audit,
    "release_audit": _evaluate_audit,
    "memo": _evaluate_memo,
    "verify_tests": _evaluate_verify_tests,
}


# Registry override seam. Empty by default. The in-flight evaluator framework
# (task_evaluators.py) can register a per-type evaluator here — consulted BEFORE
# the built-in default logic below — to supply a registry-resolved
# evaluator-key/verdict without editing this module.
OUTCOME_EVALUATORS: dict[str, Callable[[Task], TaskOutcome]] = {}


def evaluate_task_outcome(task: Task) -> TaskOutcome:
    """Return the terminal :class:`TaskOutcome` for *task*, independent of PR.

    Resolution order:

    1. A registry override in :data:`OUTCOME_EVALUATORS` (the evaluator-framework
       seam) wins outright.
    2. Unknown / future task types (no registry capability) → ``neutral`` /
       ``unknown`` so the primitive is forward-compatible.
    3. Write/PR task types (``WRITE_TASK_TYPES``) → merged-PR semantics,
       preserved exactly via :func:`_evaluate_write`.
    4. Read-only task types → the type's built-in evaluator, or ``neutral`` /
       ``read_only_unclassified`` when the type carries no honest signal yet.
    """
    task_type = _task_type(task)

    override = OUTCOME_EVALUATORS.get(task_type) if task_type else None
    if override is not None:
        return override(task)

    capability = get_task_capability(task_type) if task_type else None
    if capability is None:
        return TaskOutcome("neutral", KIND_UNKNOWN)

    if task_type in WRITE_TASK_TYPES:
        return _evaluate_write(task)

    evaluator = _READ_ONLY_EVALUATORS.get(task_type)
    if evaluator is None:
        return TaskOutcome("neutral", KIND_READ_ONLY_UNCLASSIFIED)
    return evaluator(task)


def is_successful_outcome(task: Task) -> bool:
    """Generalized success predicate — a drop-in superset of
    :func:`~app.landed_signal.is_merged_completion`.

    For write/PR task types this returns exactly ``is_merged_completion(task)``;
    for read-only types it additionally counts each type's own honest success
    signal (actionable research findings, structured audit findings, a memo
    artifact, a passing verify-tests verdict). Callers that need the richer
    ``(status, kind)`` bucketing should use :func:`evaluate_task_outcome`.
    """
    return evaluate_task_outcome(task).status == "success"
