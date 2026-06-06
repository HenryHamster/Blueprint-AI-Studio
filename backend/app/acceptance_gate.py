"""Shared, fail-closed acceptance-checking core for declared task evidence.

Blueprint historically had exactly ONE task type that declared, at plan time,
what would prove its claim true: ``compute_experiment`` and its numeric
``success_criterion`` (metric / operator / threshold), enforced fail-closed by
the metric evaluation gate (:mod:`app.task_evaluators`). Every code-producing
task type (create / fix / refactor / qa / migrate / deps / docs) instead rode
the delegated CI-green + review merge gate, which never verified the SPECIFIC
behavior the task claimed.

An optional :class:`~app.payload_schemas.AcceptanceEvidence` declaration now
rides on those code payloads. This module is the single place that:

  * holds the shared, fail-closed numeric comparison core
    (:data:`CRITERION_OPS`, :func:`coerce_number`, :func:`criterion_met`) that
    BOTH the compute success-criterion gate and the code acceptance gate reuse,
    so there is one fail-closed core rather than two;
  * evaluates a code task's declared acceptance evidence at completion
    (:func:`evaluate_code_acceptance`);
  * names the additive, non-underscore context key the verdict travels under
    (:data:`CODE_ACCEPTANCE_VERDICT_KEY`), stamps it durably
    (:func:`record_code_acceptance_verdict`), and decides — from a recorded
    verdict — whether a merged code task may still be credited as a verified
    success (:func:`acceptance_blocks_success`).

The contract is deliberately ADDITIVE and fail-safe, mirroring
:mod:`app.reproduction_scoring`: a task that declared NO acceptance evidence is
never blocked or retro-failed (the key is never written, so scoring is
unchanged); a task that DECLARED evidence which is absent / unsatisfied records
``satisfied=False`` and is honestly held back from a verified-success credit.
The gate AUGMENTS — it never replaces or double-gates — the existing CI-green +
review merge gate: it does not block the merge, it informs durable scoring the
same way the reproduce-on-HEAD verdict does.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Any, Callable

from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY
from app.models import Task
from app.payload_schemas import (
    AcceptanceEvidence,
    _looks_like_test_file,
    read_acceptance_evidence,
)

# ---------------------------------------------------------------------------
# Shared fail-closed numeric comparison core (compute + code share this).
# ---------------------------------------------------------------------------

# Closed, bounded set of comparison operators a numeric acceptance criterion may
# declare — no general expression framework, just a single numeric/threshold
# goal. Mirrors the ``Literal`` operator set on ``ComputeSuccessCriterion``.
CRITERION_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


def coerce_number(value: Any) -> float | None:
    """Return ``value`` as a float when it is a real number, else ``None``.

    ``bool`` is a subclass of ``int`` in Python, so it is explicitly rejected —
    ``True``/``False`` is never a valid measurement.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def criterion_met(value: Any, op: str, threshold: Any) -> bool:
    """Fail-closed numeric comparison shared by every acceptance gate.

    Returns ``True`` only when ``value`` and ``threshold`` are both real numbers,
    ``op`` is a recognized operator, AND the comparison holds. An absent /
    non-numeric value, an unrecognized operator, or a non-numeric threshold all
    return ``False`` — a declared goal we cannot evaluate is NEVER a free pass.
    """
    num = coerce_number(value)
    thr = coerce_number(threshold)
    fn = CRITERION_OPS.get(op)
    if num is None or thr is None or fn is None:
        return False
    return fn(num, thr)


# ---------------------------------------------------------------------------
# Code-task acceptance gate.
# ---------------------------------------------------------------------------

# Additive, non-underscore context key the verdict travels under, so it
# propagates through the standard context-copy paths exactly like
# ``target_defect_reproduction``. Absence of the key means "no acceptance
# verdict was recorded" and preserves prior bare-merge scoring exactly.
CODE_ACCEPTANCE_VERDICT_KEY = "code_acceptance_verdict"


@dataclass(frozen=True)
class AcceptanceGateVerdict:
    """Result of evaluating a code task's declared acceptance evidence.

    ``declared`` is False when the task declared no acceptance evidence at all —
    in that case ``satisfied`` is True and the verdict is never recorded
    (preserving today's behavior). When ``declared`` is True, ``satisfied``
    answers whether the declared evidence is genuinely present/exercised to the
    extent checkable at completion; a False ``satisfied`` is the honest
    fail-closed outcome.
    """

    declared: bool
    satisfied: bool
    kind: str | None
    reason: str


def _changed_files(task: Task) -> list[str]:
    """The PR's changed-file paths recorded on the task, as a list of strings."""
    raw = (task.context or {}).get("_pr_changed_files")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str) and p.strip()]


def _is_test_path(path: str) -> bool:
    """Whether ``path`` names a test file (under a tests/testing dir or by name)."""
    return _looks_like_test_file(path)


def _paths_match(changed: str, declared: str) -> bool:
    """Whether a changed-file path refers to the declared test target.

    Tolerant of leading-directory differences between the agent-reported changed
    paths and the declared ``test_path`` (one may be repo-relative, the other a
    subpath): an exact match, a suffix match in either direction, or a basename
    match all count.
    """
    changed = changed.strip().lstrip("./")
    declared = declared.strip().lstrip("./")
    if not changed or not declared:
        return False
    if changed == declared:
        return True
    if changed.endswith("/" + declared) or declared.endswith("/" + changed):
        return True
    return changed.rsplit("/", 1)[-1] == declared.rsplit("/", 1)[-1]


def _merge_was_test_verified(task: Task) -> bool:
    """Whether the merge was gated by a REAL test verdict (no fall-open).

    The no-CI merge gate stamps ``review_loop["unverified_merge_reason"]`` when
    it legitimately "falls open" and merges WITHOUT running real tests. Absence
    of that reason means a real CI / test verdict gated the merge — i.e. a
    ``named_test`` that ran green did so under genuine verification. Mirrors the
    ``"verified"`` arm of :func:`app.landed_signal.merge_verification_status`
    without requiring the task to already be a terminal merged completion.
    """
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return True
    reason = review_loop.get(UNVERIFIED_MERGE_REASON_KEY)
    return not (isinstance(reason, str) and reason)


def _evaluate_named_test(
    evidence: AcceptanceEvidence, changed: list[str], verified: bool
) -> AcceptanceGateVerdict:
    """Fail-closed gate for a ``named_test`` acceptance declaration.

    Passes only when the named test (1) is among the PR's changed files — it was
    actually added/modified by this PR rather than merely claimed — (2) ran green
    under a real merge-test verdict, and (3) the PR also ships a NON-test
    production change for the test to exercise (the changed seam). A test-only
    diff fails: a test that ships no changed production code cannot be exercising
    the changed seam, and an always-true assertion added in isolation is exactly
    that shape. Trivial / placeholder test references are already rejected
    fail-closed at declaration time by ``AcceptanceEvidence`` validation.
    """
    declared_path = (evidence.test_path or "").partition("::")[0].strip()
    test_present = any(_paths_match(cf, declared_path) for cf in changed)
    if not test_present:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} is not among the PR's "
            "changed files — the claimed test was not added or modified here",
        )
    if not verified:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} did not run green under a "
            "real merge-test verdict (the merge fell open without verification)",
        )
    seam_files = [cf for cf in changed if not _is_test_path(cf)]
    if not seam_files:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} ships no production code "
            "change it could exercise (the diff is test-only)",
        )
    return AcceptanceGateVerdict(
        True,
        True,
        evidence.kind,
        f"named_test {declared_path!r} was added/modified, ran green under a "
        f"verified merge, and exercises a changed production seam "
        f"({len(seam_files)} non-test file(s))",
    )


def evaluate_code_acceptance(task: Task) -> AcceptanceGateVerdict:
    """Evaluate ``task``'s declared acceptance evidence, fail-closed.

    Self-contained from the task: reads the declaration from the payload, the
    PR's changed files from ``context['_pr_changed_files']``, and whether the
    merge was test-verified from the no-CI gate's recorded reason. A task with no
    declaration returns a non-recorded passing verdict (``declared=False``),
    preserving today's behavior exactly.
    """
    evidence = read_acceptance_evidence(task.first_payload)
    if evidence is None:
        return AcceptanceGateVerdict(
            False, True, None, "no acceptance evidence declared"
        )

    changed = _changed_files(task)
    verified = _merge_was_test_verified(task)

    if evidence.kind == "named_test":
        return _evaluate_named_test(evidence, changed, verified)

    # observable_behavior / downstream_artifact carry no machine-checkable
    # assertion the backend can independently re-run at completion. To the extent
    # checkable, require that the task actually shipped a real diff (a non-empty
    # changed-file set) — a declared behavior/artifact with an empty diff shipped
    # nothing to exercise, so it fails closed. The declaration's substance
    # (non-trivial description / artifact_id) is already enforced at validation.
    if evidence.kind in ("observable_behavior", "downstream_artifact"):
        if not changed:
            return AcceptanceGateVerdict(
                True,
                False,
                evidence.kind,
                f"declared {evidence.kind} acceptance evidence but the task "
                "shipped no changed files to exercise it",
            )
        return AcceptanceGateVerdict(
            True,
            True,
            evidence.kind,
            f"declared {evidence.kind} acceptance evidence shipped with a real "
            f"diff ({len(changed)} changed file(s))",
        )

    # Unknown/future kind — fail closed rather than silently pass.
    return AcceptanceGateVerdict(
        True, False, evidence.kind, f"unrecognized acceptance kind {evidence.kind!r}"
    )


def record_code_acceptance_verdict(task: Task) -> AcceptanceGateVerdict | None:
    """Evaluate and durably stamp ``task``'s acceptance verdict on its context.

    No-op (returns ``None``, writes nothing) when the task declared no acceptance
    evidence, so undeclared/legacy tasks keep their context untouched and their
    scoring unchanged. Otherwise records ``{satisfied, kind, reason}`` under
    :data:`CODE_ACCEPTANCE_VERDICT_KEY` so later scoring
    (:func:`acceptance_blocks_success`) can consume it. The caller is responsible
    for committing.
    """
    verdict = evaluate_code_acceptance(task)
    if not verdict.declared:
        return None
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": verdict.satisfied,
        "kind": verdict.kind,
        "reason": verdict.reason,
    }
    task.context = ctx
    return verdict


def acceptance_blocks_success(recorded: Any) -> bool:
    """Whether a recorded acceptance verdict blocks crediting a merge as success.

    ``True`` only when a verdict was recorded AND it is explicitly unsatisfied.
    Absence of the key (``None``) — the case for every task that declared no
    acceptance evidence, plus all legacy rows — returns ``False`` so those tasks
    keep their current bare-merge success. Additive and backward-compatible,
    mirroring :func:`app.reproduction_scoring.reproduction_blocks_success`.
    """
    if not isinstance(recorded, dict):
        return False
    return recorded.get("satisfied") is False
