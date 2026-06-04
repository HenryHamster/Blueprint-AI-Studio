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
KIND_COMPUTE_RESULT = "compute_result"
KIND_KNOWLEDGE_DOCUMENT = "knowledge_document"
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


# Terminal compute-job outcomes that are intentional, non-failing ends to an
# experiment rather than a compute error: a budget teardown
# (SPEND_CAP_EXCEEDED), a deadline teardown (TIMED_OUT), and an operator kill
# (CANCELLED). They cleared the completion gate — the job produced a costed,
# terminal outcome — so they are ``neutral`` (the experiment ran but yielded no
# success/failure signal of its own), not ``failure``.
_COMPUTE_NEUTRAL_OUTCOMES = frozenset(
    {"SPEND_CAP_EXCEEDED", "TIMED_OUT", "CANCELLED"}
)


def _compute_result(task: Task) -> dict | None:
    """Best-effort read of a compute_experiment task's durable result dict.

    ``_handle_compute_experiment_completion`` stores
    ``plan_output['compute_result'] = {'outcome', 'final_cost_usd'}`` on a
    clean terminal completion. Reads task-locally (deferred-column safe) and
    returns ``None`` when no result is present.
    """
    raw_plan_output = _task_local_attr(task, "plan_output")
    if not isinstance(raw_plan_output, dict):
        return None
    result = raw_plan_output.get("compute_result")
    return result if isinstance(result, dict) else None


def _evaluate_compute_experiment(task: Task) -> TaskOutcome:
    """Outcome for a compute_experiment task — judged on its terminal job outcome.

    The honest terminal signal is the compute job's ``outcome`` stored in
    ``plan_output['compute_result']`` by the completion gate. ``SUCCESS`` is a
    ``success``; ``FAILURE`` is a ``failure``; the intentional terminations
    (``SPEND_CAP_EXCEEDED`` budget kill, ``TIMED_OUT`` deadline kill,
    ``CANCELLED`` operator kill) are ``neutral`` — they cleared the gate as
    deliberate terminal outcomes, not crashes. When no ``compute_result`` is
    present (e.g. the gate failed the task and it never stored one), fall back
    to the direction-metrics failure notion: ``failure`` if it counts as a
    genuine failure, else ``neutral``.
    """
    result = _compute_result(task)
    if result is not None:
        outcome = str(result.get("outcome") or "").strip().upper()
        if outcome == "SUCCESS":
            return TaskOutcome("success", KIND_COMPUTE_RESULT)
        if outcome == "FAILURE":
            return TaskOutcome("failure", KIND_COMPUTE_RESULT)
        if outcome in _COMPUTE_NEUTRAL_OUTCOMES:
            return TaskOutcome("neutral", KIND_COMPUTE_RESULT)
    if _counts_as_failure(task):
        return TaskOutcome("failure", KIND_COMPUTE_RESULT)
    return TaskOutcome("neutral", KIND_COMPUTE_RESULT)


def _knowledge_document(task: Task) -> dict | None:
    """Best-effort read of a web_research task's durable knowledge-document mirror.

    ``knowledge_completion._persist_document`` mirrors a compact copy of the
    persisted :class:`~app.models.KnowledgeDocument` into
    ``plan_output['knowledge_document']`` (``title`` / ``summary`` / ``topic`` /
    ``tags`` / ``citation_count``) on a clean COMPLETED run, and writes it ONLY
    on that path — a FAILED gate never stores it. Reads task-locally
    (deferred-column safe) and returns ``None`` when no mirror is present.
    """
    raw_plan_output = _task_local_attr(task, "plan_output")
    if not isinstance(raw_plan_output, dict):
        return None
    mirror = raw_plan_output.get("knowledge_document")
    return mirror if isinstance(mirror, dict) else None


def _evaluate_web_research(task: Task) -> TaskOutcome:
    """Outcome for a web_research task — judged on its durable knowledge document.

    The honest terminal signal is the persisted ``KnowledgeDocument``, mirrored
    onto ``plan_output['knowledge_document']`` by the completion handler only
    after the forward-quality gate passes. Presence of that mirror is the
    success signal — the document cleared the gate and is durable, even if some
    mirrored text fields are blank. A FAILED/CANCELLED run (the gate fails
    closed) is a ``failure``; a COMPLETED run with no mirror degrades to
    ``neutral`` (terminal but no durable artifact).
    """
    if _counts_as_failure(task):
        return TaskOutcome("failure", KIND_KNOWLEDGE_DOCUMENT)
    mirror = _knowledge_document(task)
    if mirror is not None:
        return TaskOutcome("success", KIND_KNOWLEDGE_DOCUMENT)
    return TaskOutcome("neutral", KIND_KNOWLEDGE_DOCUMENT)


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
    "compute_experiment": _evaluate_compute_experiment,
    "web_research": _evaluate_web_research,
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


# ---------------------------------------------------------------------------
# Operator-facing evaluation summaries
# ---------------------------------------------------------------------------
#
# Where :func:`evaluate_task_outcome` answers *was the output good* with a
# single (status, kind) verdict for analytics, the SUMMARY_BUILDERS registry
# answers *what did the output say* for operator surfaces (Task Detail,
# Research Browser). Each builder turns a terminal task's type-specific artifact
# into a normalized ``{status, verdict, evidence[], highlights[]}`` dict that the
# admin endpoints wrap into the ``TaskEvaluationSummary`` wire schema.
#
# Registering a new evaluator-backed task type is one entry in SUMMARY_BUILDERS
# (or relying on the shared findings builder fallback) rather than a new
# ``if task_type == …`` branch in admin.py.


@dataclass(frozen=True)
class SummaryContext:
    """Side-channel artifacts a summary builder may need beyond the Task row.

    ``merge_test_result`` is the parsed MERGE_TEST_RESULT artifact for a
    verify_tests task — it lives off-task (the admin endpoint loads it) so the
    builder cannot read it from ``plan_output``.
    """

    merge_test_result: dict[str, Any] | None = None


def _summary_status_label(task: Task) -> str:
    status = getattr(task.status, "value", task.status)
    return str(status)


def _trim_summary_text(value: Any, limit: int = 200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _summary_item(label: str, value: Any) -> dict[str, str]:
    return {"label": label, "value": _trim_summary_text(value)}


def _summary_dict(
    task: Task,
    verdict: str,
    evidence: list[dict[str, str]],
    highlights: list[dict[str, str]],
) -> dict[str, Any]:
    """Assemble the normalized summary dict with the canonical truncation."""
    return {
        "status": _summary_status_label(task),
        "verdict": verdict,
        "evidence": evidence[:4],
        "highlights": highlights[:3],
    }


def _build_verify_tests_summary(task: Task, ctx: SummaryContext) -> dict[str, Any] | None:
    verdict = "undetected"
    evidence: list[dict[str, str]] = []
    highlights: list[dict[str, str]] = []
    result = ctx.merge_test_result or {}
    tests = result.get("tests") if isinstance(result.get("tests"), list) else []
    output = _trim_summary_text(result.get("output"), 240)
    detected = result.get("detected")
    passed = result.get("passed")
    if detected is False:
        verdict = "undetected"
    elif passed:
        verdict = "pass"
    elif detected is True:
        verdict = "fail"

    if detected is not None:
        evidence.append(_summary_item("Detected suite", "yes" if detected else "no"))
    if isinstance(tests, list) and tests:
        pass_count = sum(1 for t in tests if isinstance(t, dict) and t.get("status") == "pass")
        fail_count = sum(1 for t in tests if isinstance(t, dict) and t.get("status") == "fail")
        evidence.append(_summary_item("Tests", f"{len(tests)} total, {pass_count} passed, {fail_count} failed"))
        for test in tests[:3]:
            if not isinstance(test, dict):
                continue
            name = str(test.get("name") or test.get("suite") or "test").strip()
            status_text = str(test.get("status") or "unknown").strip()
            message = _trim_summary_text(test.get("message") or "", 80)
            highlights.append(
                _summary_item(name, f"{status_text}{f' - {message}' if message else ''}")
            )
    if output:
        evidence.append(_summary_item("Output", output))
    if not evidence and result:
        evidence.append(_summary_item("Result", "MERGE_TEST_RESULT captured"))
    if not highlights and output:
        highlights.append(_summary_item("Output", output))
    return _summary_dict(task, verdict, evidence, highlights)


def _build_memo_summary(task: Task, ctx: SummaryContext) -> dict[str, Any] | None:
    raw_plan_output = _task_local_attr(task, "plan_output")
    plan_output = raw_plan_output if isinstance(raw_plan_output, dict) else None
    memo = plan_output.get("memo") if isinstance(plan_output, dict) else None
    if not isinstance(memo, dict):
        return None
    summary = _trim_summary_text(memo.get("summary"), 200)
    reasoning = _trim_summary_text(memo.get("reasoning"), 240)
    citations = memo.get("citations") if isinstance(memo.get("citations"), list) else []
    tags = memo.get("tags") if isinstance(memo.get("tags"), list) else []
    evidence: list[dict[str, str]] = []
    if summary:
        evidence.append(_summary_item("Summary", summary))
    if reasoning:
        evidence.append(_summary_item("Reasoning", reasoning))
    highlights: list[dict[str, str]] = []
    if citations:
        highlights.append(_summary_item("Citations", ", ".join(str(c) for c in citations[:3])))
    if tags:
        highlights.append(_summary_item("Tags", ", ".join(str(t) for t in tags[:4])))
    return _summary_dict(task, "memo", evidence, highlights)


def _build_self_audit_summary(task: Task, ctx: SummaryContext) -> dict[str, Any] | None:
    raw_plan_output = _task_local_attr(task, "plan_output")
    findings = raw_plan_output if isinstance(raw_plan_output, list) else []
    normalized = [f for f in findings if isinstance(f, dict)]
    if not normalized:
        return None
    severity_order = {"ship": 0, "fundamentals": 1, "audit": 2, "pause": 3}

    def _rank(finding: dict[str, Any]) -> int:
        mode = str(finding.get("recommended_mode") or "ship").strip().lower()
        return severity_order.get(mode, 0)

    worst = max(normalized, key=_rank)
    verdict = str(worst.get("recommended_mode") or "ship").strip().lower() or "ship"
    evidence = [
        _summary_item("Systems", len(normalized)),
        _summary_item("Worst score", worst.get("score", "n/a")),
        _summary_item("Worst mode", verdict),
    ]
    highlights: list[dict[str, str]] = []
    for finding in sorted(
        normalized,
        key=lambda f: (int(f.get("score", 0)) if str(f.get("score", "")).isdigit() else 0),
    )[:3]:
        system_id = str(finding.get("system_id") or "system").strip()
        score = finding.get("score", "n/a")
        mode = str(finding.get("recommended_mode") or "ship").strip().lower() or "ship"
        highlights.append(_summary_item(system_id, f"{mode} · score {score}"))
    return _summary_dict(task, verdict, evidence, highlights)


def _build_compute_experiment_summary(
    task: Task, ctx: SummaryContext
) -> dict[str, Any] | None:
    """Operator summary for a compute_experiment task — the terminal job result.

    Reads the durable ``plan_output['compute_result']`` (``outcome`` +
    ``final_cost_usd``) and surfaces the outcome label as the verdict with the
    outcome and measured spend as evidence. Returns ``None`` when no
    ``compute_result`` is present so the caller falls through to the no-summary
    contract (the same way the other builders degrade on a missing artifact).
    """
    result = _compute_result(task)
    if result is None:
        return None
    outcome = str(result.get("outcome") or "unknown").strip() or "unknown"
    verdict = outcome.lower()
    evidence = [_summary_item("Outcome", outcome)]
    cost = result.get("final_cost_usd")
    if cost is not None:
        evidence.append(_summary_item("Final cost", f"${cost}"))
    highlights = [_summary_item("Outcome", outcome)]
    return _summary_dict(task, verdict, evidence, highlights)


def _build_findings_summary(task: Task, ctx: SummaryContext) -> dict[str, Any] | None:
    """Shared builder for the structured-findings task types (security_audit,
    perf, release_audit, …) and the fallback for any unregistered type whose
    ``plan_output`` carries a ``findings``/``issues`` list."""
    raw_plan_output = _task_local_attr(task, "plan_output")
    plan_output = raw_plan_output if isinstance(raw_plan_output, dict) else None
    findings = None
    if isinstance(plan_output, dict):
        findings = plan_output.get("findings") or plan_output.get("issues")
    if not isinstance(findings, list) or not findings:
        return None
    normalized_findings = [f for f in findings if isinstance(f, dict)]
    if not normalized_findings:
        return None
    actionable = [
        f for f in normalized_findings if str(f.get("severity") or "").lower() in {"critical", "high"}
    ]
    verdict = "clean" if not actionable else "actionable"
    evidence = [
        _summary_item("Findings", len(normalized_findings)),
        _summary_item("Actionable", len(actionable)),
    ]
    highlights: list[dict[str, str]] = []
    for finding in normalized_findings[:3]:
        severity = str(finding.get("severity") or "info").strip().lower()
        title = str(finding.get("title") or finding.get("description") or "finding").strip()
        highlights.append(_summary_item(title, severity))
    return _summary_dict(task, verdict, evidence, highlights)


def _build_web_research_summary(
    task: Task, ctx: SummaryContext
) -> dict[str, Any] | None:
    """Operator summary for a web_research task — the durable knowledge document.

    Reads the compact ``plan_output['knowledge_document']`` mirror (``title`` /
    ``summary`` / ``topic`` / ``tags`` / ``citation_count``) the completion
    handler stores on a passing run and surfaces it as a ``recorded`` verdict.
    Any populated mirrored fields become evidence/highlights; the summary still
    records the durable document when title is blank because persistence itself
    is the honest signal. Returns ``None`` only when no mirror is present so
    the caller falls through to the no-summary contract (the same way the other
    builders degrade on a missing artifact).
    """
    mirror = _knowledge_document(task)
    if mirror is None:
        return None
    evidence: list[dict[str, str]] = []
    title = _trim_summary_text(mirror.get("title"), 200)
    if title:
        evidence.append(_summary_item("Title", title))
    summary = _trim_summary_text(mirror.get("summary"), 240)
    if summary:
        evidence.append(_summary_item("Summary", summary))
    topic = _trim_summary_text(mirror.get("topic"), 120)
    if topic:
        evidence.append(_summary_item("Topic", topic))
    citation_count = mirror.get("citation_count")
    if citation_count is not None:
        evidence.append(_summary_item("Citations", citation_count))
    tags = mirror.get("tags") if isinstance(mirror.get("tags"), list) else []
    highlights: list[dict[str, str]] = []
    if tags:
        highlights.append(_summary_item("Tags", ", ".join(str(t) for t in tags[:4])))
    if not evidence:
        evidence.append(_summary_item("Document", "Persisted"))
    return _summary_dict(task, "recorded", evidence, highlights)


# Per-type summary builders, keyed by task-type value. Types absent from this
# map fall back to the shared findings builder (which returns ``None`` when the
# task carries no structured findings) — so security_audit / perf / release_audit
# and any future findings-shaped type are handled without a new branch.
SUMMARY_BUILDERS: dict[str, Callable[[Task, SummaryContext], dict[str, Any] | None]] = {
    "verify_tests": _build_verify_tests_summary,
    "memo": _build_memo_summary,
    "self_audit": _build_self_audit_summary,
    "security_audit": _build_findings_summary,
    "perf": _build_findings_summary,
    "release_audit": _build_findings_summary,
    "compute_experiment": _build_compute_experiment_summary,
    "web_research": _build_web_research_summary,
}


def build_task_evaluation_summary(
    task: Task,
    *,
    merge_test_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the normalized operator-facing evaluation summary for *task*.

    Resolves the builder for the task's type from :data:`SUMMARY_BUILDERS`,
    falling back to the shared findings builder for any type without a dedicated
    entry. Returns ``None`` when the type produces no renderable summary (e.g. a
    task with no structured artifact), exactly as the previous admin-local
    dispatcher did. The returned dict is ``{status, verdict, evidence,
    highlights}`` with ``evidence``/``highlights`` already truncated; the caller
    wraps it into the ``TaskEvaluationSummary`` wire schema.
    """
    task_type = _task_type(task) or ""
    ctx = SummaryContext(merge_test_result=merge_test_result)
    builder = SUMMARY_BUILDERS.get(task_type, _build_findings_summary)
    return builder(task, ctx)
