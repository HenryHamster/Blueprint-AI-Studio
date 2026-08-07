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
the reproduction-scoring leaf module: a task that declared NO acceptance evidence is
never blocked or retro-failed (the key is never written, so scoring is
unchanged); a task that DECLARED evidence which is absent / unsatisfied records
``satisfied=False`` and is honestly held back from a verified-success credit.
The gate AUGMENTS — it never replaces or double-gates — the existing CI-green +
review merge gate: it does not block the merge, it informs durable scoring the
same way the reproduce-on-HEAD verdict does.
"""

from __future__ import annotations

import logging
import uuid
import operator
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from app.ci_resolution import is_degraded_unverified_reason
from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY
from app.landed_signal import merge_verification_status
from sqlalchemy import String, and_, cast, exists, not_, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.time_utils import _utcnow

from app.artifact_prerequisites import ARTIFACT_PREREQUISITES_KEY
from app.artifact_prerequisites import downstream_artifact_logical_key
from app.models import Artifact
from app.models import ArtifactConsumption
from app.models import CriterionEvidence
from app.models import Project
from app.models import Task
from app.models import TaskStatus
from app.payload_schemas import (
    AcceptanceEvidence,
    _looks_like_test_file,
    read_acceptance_evidence,
)
from app.viewport_evidence import resolve_visual_evidence

logger = logging.getLogger(__name__)

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
# propagates through the standard context-copy paths (a pattern previously
# used by the now-removed reproduction-scoring module). Absence of the key
# means "no acceptance verdict was recorded" and preserves prior bare-merge
# scoring exactly.
CODE_ACCEPTANCE_VERDICT_KEY = "code_acceptance_verdict"

# Durable top-level context key where a PRE-MERGE structural acceptance finding
# is stamped while a PR is still open. Lives at the TOP level (not inside
# ``review_loop``) so it can never flip the merge-verification semantics that an
# empty ``review_loop`` dict signals. Absence means no structural defect recorded.
PREMERGE_ACCEPTANCE_FINDING_KEY = "premerge_acceptance_finding"

# Terminal marker for the missing-from-diff backfill class: stamped by the
# backfill sweep when changed-file provenance WAS present at examination time
# and re-evaluation still could not locate the declared test (the task declared
# a named_test it genuinely never shipped). Without it, permanently-unprovable
# tasks are re-selected by every sweep and — being among the oldest rows —
# eventually consume the entire LIMIT-200 window. Cleared alongside
# ``_backfill_checked_at`` whenever fresh provenance arrives, so a late
# re-stamp still re-admits the task.
BACKFILL_MISSING_DIFF_TERMINAL_KEY = "_backfill_missing_diff_terminal_at"

# Context key where the verify-tests handler stores the coverage measurement
# result for the coverage_delta acceptance gate to consume. The value is a dict
# with at least ``{"coverage_pct": <float>}``.
#
# ``coverage_pct`` represents LINE / STATEMENT coverage (the fraction of source
# lines exercised by the test suite), NOT the test pass rate. When coverage
# instrumentation (pytest-cov, c8) is active, the value is the actual line
# coverage percentage from the instrumentation tool. When no instrumentation is
# active, the orchestrator falls back to the "test suite execution coverage" —
# the percentage of tests that passed — as a less meaningful but always-available
# proxy.
TEST_COVERAGE_RESULT_KEY = "test_coverage_result"

ACCEPTANCE_AGGREGATE_BUCKETS: tuple[str, ...] = (
    "proven",
    "satisfied_unproven",
    "inferred_test_cochange",
    "declared_unsatisfied",
    "undeclared",
    "merged_unverified_degraded",
    "malformed",
)


@dataclass(frozen=True)
class AcceptanceGateVerdict:
    """Result of evaluating a code task's declared acceptance evidence.

    ``declared`` is False when the task declared no acceptance evidence at all —
    in that case ``satisfied`` is True and the verdict is never recorded
    (preserving today's behavior). When ``declared`` is True, ``satisfied``
    answers whether the declared evidence is genuinely present/exercised to the
    extent checkable at completion; a False ``satisfied`` is the honest
    fail-closed outcome.

    ``proven`` is a STRICTER axis than ``satisfied``: it is True only when the
    claim was machine-checked — a satisfied ``named_test`` (the test is in the
    diff, ran green under a real merge-test verdict, and exercises a changed
    production seam), a satisfied ``visual`` evidence pair whose uploaded
    viewport screenshot matches the declared component/width, or a consumed
    ``downstream_artifact``. A satisfied ``observable_behavior`` is
    ``satisfied=True`` but ``proven=False`` (it proves only that a diff shipped,
    not that the specific claim holds), and every fail-closed / unsatisfied /
    undeclared case is ``proven=False``. ``satisfied`` semantics and every
    downstream consumer of it are unchanged; ``proven`` is purely additive so
    callers can distinguish machine-checked proof from merely-delivered work.
    """

    declared: bool
    satisfied: bool
    kind: str | None
    reason: str
    proven: bool = False
    proof_ref: str | None = None


# Maximum number of changed-file paths stored in the ``_pr_changed_files``
# context signal. The stored list is capped for size, but the acceptance-relevant
# summary (has-test / has-prod / truncated sentinels) is computed over the FULL
# list so downstream classification stays truncation-proof.
_CHANGED_FILES_STAMP_CAP = 50


def _changed_files(task: Task) -> list[str]:
    """The PR's changed-file paths recorded on the task, as a list of strings."""
    raw = (task.context or {}).get("_pr_changed_files")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str) and p.strip()]


def stamp_changed_files_signal(
    paths: list[str], *, github_verified: bool = False
) -> dict[str, Any]:
    """Non-lossy acceptance signal for a PR's changed-file paths.

    ``_pr_changed_files`` is truncated to 50 entries when stamped onto task
    context, but the acceptance classifier and the ``named_test`` gate treat that
    list as complete. A >50-file PR could drop every test path (or the declared
    test file) beyond index 50 and be silently misclassified — flipping
    ``inferred_test_cochange`` to ``undeclared`` or a would-be proof to a merge
    that fails closed. Compute the acceptance-relevant summary over the FULL list
    here (whether any test path and any production path shipped, and whether the
    stored list was truncated) so downstream classification is truncation-proof.

    When ``github_verified=True``, the caller sourced the file list directly from
    GitHub's API rather than from the agent's self-report. The
    ``_pr_changed_files_github_verified`` sentinel is stamped alongside so the
    ``named_test`` gate can distinguish authoritative absence (hard fail) from an
    agent self-report truncation (soft credit).
    """
    str_paths = [p for p in paths if isinstance(p, str) and p.strip()]
    signal: dict[str, Any] = {
        "_pr_changed_files": str_paths[:_CHANGED_FILES_STAMP_CAP],
        "_pr_changed_files_has_test": any(_is_test_path(p) for p in str_paths),
        "_pr_changed_files_has_prod": any(not _is_test_path(p) for p in str_paths),
    }
    if len(str_paths) > _CHANGED_FILES_STAMP_CAP:
        signal["_pr_changed_files_truncated"] = True
    if github_verified:
        signal["_pr_changed_files_github_verified"] = True
    return signal


# Sentinel marking a child's ``_pr_changed_files`` as INHERITED from its rollup
# parent's combined diff rather than sourced from the child's own PR. The
# parent's diff is the union of every sibling's changes, so a child's declared
# test that actually lives in a SIBLING's slice would appear "present" here — the
# named_test gate must therefore cap parent-derived credit at the unproven
# (inferred) tier and never mint machine ``proven`` credit from it.
PARENT_DERIVED_CHANGED_FILES_KEY = "_pr_changed_files_from_rollup_parent"


def stamp_parent_derived_changed_files_signal(paths: list[str]) -> dict[str, Any]:
    """Changed-file signal for a rollup child inheriting its parent's combined diff.

    Reuses :func:`stamp_changed_files_signal` for the non-lossy summary but marks
    the list parent-derived and deliberately does NOT set the
    ``_pr_changed_files_github_verified`` sentinel: the combined diff is a
    superset (the union of all siblings' changes), so a child's declared test that
    lives in a sibling's slice must not falsely earn full machine-``proven``
    credit. Parent-derived credit is capped at the satisfied/unproven tier by
    :func:`_evaluate_named_test`.
    """
    signal = stamp_changed_files_signal(paths, github_verified=False)
    signal[PARENT_DERIVED_CHANGED_FILES_KEY] = True
    return signal


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


# Cross-cutting/shared test files that validate invariants or integrations
# spanning multiple production modules (not tied to any single module).
# These are exempt from the per-module stem-match heuristic in
# _test_semantically_grounded because their name does not follow the
# test_<module>.py convention — add new cross-cutting entries here.
_CROSS_CUTTING_TEST_FILES: frozenset[str] = frozenset({
    "backend/testing/test_invariants.py",
    "backend/testing/test_admin_auth_invariant.py",
    "backend/testing/test_agent_branch_invariants.py",
    "backend/testing/test_backmerge_invariants.py",
    "backend/testing/test_c5_approval_gate_invariant.py",
    "backend/testing/test_event_type_invariants.py",
    "backend/testing/test_schema_invariants.py",
    "backend/testing/test_workday_resident_invariants.py",
    # Validates the CI-compliance fetch/gate + cap-bump remediation flow, which
    # spans ci_compliance, strategic_spawning, strategic_completion,
    # release_version and models — no single ``ci_compliance_gate.py`` module
    # exists, so its stem cannot ground via the per-module heuristic.
    "backend/testing/test_ci_compliance_gate.py",
})


def get_cross_cutting_test_files() -> frozenset[str]:
    """Return the set of cross-cutting test file paths exempt from stem-match."""
    return _CROSS_CUTTING_TEST_FILES


# vitest/jest test-file naming: ``<stem>.test.tsx`` / ``<stem>.spec.mts`` →
# ``<stem>``. The recognized suffix set mirrors the JS/TS alternatives in
# :data:`app.payload_schemas._TEST_FILE_BASENAME_RE` so test-file *detection*
# and semantic *grounding* cannot drift apart.
_JS_TS_TEST_BASENAME_RE = re.compile(r"^(?P<stem>.+)\.(?:test|spec)\.[cm]?[jt]sx?$")

# Suffix-style test-file naming: ``<stem>_test.py`` / ``<stem>_test.go`` /
# ``<stem>_test.exs`` / ``<stem>_spec.rb`` → ``<stem>``. These conventions are
# RECOGNIZED by test-file *detection* (:data:`app.payload_schemas._TEST_FILE_BASENAME_RE`)
# but were previously invisible to stem *grounding*, which only understood the
# ``test_<module>`` prefix. Mirroring the suffix set here keeps detection and
# grounding from drifting apart (the same rationale as the JS/TS regex above),
# so a legitimately-declared ``foo_test.py`` co-changed with ``foo.py`` can earn
# machine-checked proof instead of silently failing closed.
_SUFFIX_TEST_BASENAME_RE = re.compile(r"^(?P<stem>.+)_(?:test\.(?:py|go|exs?)|spec\.rb)$")


def _test_module_stem(test_basename: str) -> str | None:
    """Extract the production-module stem a test file is named after.

    Understands the Python ``test_<module>.py`` prefix convention (→ ``module``),
    the vitest/jest ``<module>.test.tsx`` / ``<module>.spec.ts`` conventions
    (→ ``module``), and the suffix conventions ``<module>_test.py`` /
    ``<module>_test.go`` / ``<module>_test.exs`` / ``<module>_spec.rb``
    (→ ``module``) — every shape test-file detection already recognizes. For the
    Python prefix and suffix conventions, a double-underscore variant qualifier
    (``test_<module>__<variant>.py`` / ``<module>__<variant>_test.py``) is
    truncated at the first ``__`` so the stem names the BASE module under test:
    ``test_release_qualifier__sweep_stuck_qualifications.py`` →
    ``release_qualifier``. ``test_basename`` is the file basename *including*
    its extension.

    Returns ``None`` (fail closed) for any name that follows no recognized
    convention (e.g. ``random.py``), so the caller cannot grant machine-checked
    proof it is unable to ground.
    """
    js_match = _JS_TS_TEST_BASENAME_RE.match(test_basename)
    if js_match:
        return js_match.group("stem") or None
    # Prefix convention: strip the file extension, then the leading ``test_``.
    py_stem = test_basename.rsplit(".", 1)[0]
    if py_stem.startswith("test_"):
        module = py_stem[len("test_"):]
        # A ``test_<module>__<variant>.py`` name names its BASE module — the
        # focus qualifier after the first ``__`` is not the module under test.
        return (module.split("__", 1)[0]) or None
    # Suffix convention: ``<module>_test.<ext>`` / ``<module>_spec.rb`` — a real,
    # detection-recognized test-file shape the prefix branch cannot capture.
    suffix_match = _SUFFIX_TEST_BASENAME_RE.match(test_basename)
    if suffix_match:
        return (suffix_match.group("stem") or "").split("__", 1)[0] or None
    return None


def _stem_family_matches(stem: str, prod_basename: str) -> bool:
    """Whether a test module ``stem`` and a changed production ``prod_basename``
    belong to the same underscore-prefix family.

    Three relations count as the same family:

      * exact equality (``release_qualifier`` vs ``release_qualifier``);
      * the production basename extends the stem with an underscore suffix
        (``release_qualifier`` vs ``release_qualifier_sweep``) — a base-named
        test co-changed with an extracted submodule;
      * the stem extends the production basename with an underscore suffix
        (``release_qualifier_sweep`` vs ``release_qualifier``) — a
        submodule-named test co-changed with the surviving base.

    A shared short prefix alone is NOT a match unless one side is an
    underscore-prefix extension of the other, so unrelated modules with an
    accidental common prefix still fail closed.
    """
    if stem == prod_basename:
        return True
    if prod_basename.startswith(stem + "_"):
        return True
    return bool(stem.startswith(prod_basename + "_"))


def _test_semantically_grounded(
    declared_test_path: str, seam_files: list[str]
) -> bool:
    """Whether a declared test path semantically corresponds to a changed seam.

    A heuristic that the test file's module stem must family-match at least one
    changed non-test file's module name. The stem is derived from the Python
    ``test_<module>.py`` prefix convention (with ``test_<module>__<variant>.py``
    truncating at the first ``__`` to the base module), the vitest/jest
    ``<module>.test.tsx`` / ``<module>.spec.ts`` conventions, or the suffix
    conventions ``<module>_test.py`` / ``<module>_spec.rb`` (see
    :func:`_test_module_stem`). Family-match is exact equality OR an
    underscore-prefix extension in either direction (see
    :func:`_stem_family_matches`): a declaration of ``test_acceptance_gate.py``
    must co-occur with a change to ``acceptance_gate.py``; a declaration of
    ``test_release_qualifier__sweep_stuck_qualifications.py`` may co-occur with
    a change to ``release_qualifier.py`` (its base module) or an extracted
    ``release_qualifier_sweep.py`` submodule; a declaration of
    ``ComputeJobsPanel.test.tsx`` must co-occur with a change to
    ``ComputeJobsPanel.tsx`` (or ``.ts``) — not just any production file.

    Files that follow no recognized test-naming convention (e.g. a bare
    ``random.py``) fail closed: the heuristic cannot ground them, so the gate
    errs on the side of not granting machine-checked proof.
    """
    test_basename = declared_test_path.rsplit("/", 1)[-1]
    test_stem = _test_module_stem(test_basename)
    if test_stem:
        for path in seam_files:
            prod_basename = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if _stem_family_matches(test_stem, prod_basename):
                return True
    # Cross-cutting test files (e.g., test_invariants.py) do not map to a
    # single production module — accept them as intrinsically grounded when
    # any non-test production seam exists (already verified by the caller).
    for known in _CROSS_CUTTING_TEST_FILES:
        if _paths_match(known, declared_test_path):
            return True
    return False


def _merge_was_test_verified(task: Task) -> bool:
    """Whether the merge was gated by a REAL test verdict.

    Mirrors the canonical ``landed_signal.is_verified_merge`` predicate (and
    this module's own ``_context_merge_verification_status``): the no-CI merge
    gate stamps ``review_loop["unverified_merge_reason"]`` when it legitimately
    "falls open" and merges WITHOUT running real tests. Presence of that reason
    means the merge was NOT test-verified — returns False. An ABSENT or
    malformed review_loop carries no fall-open reason, so the merge is treated
    as VERIFIED — exactly the fail-safe default every other consumer uses. Only
    an explicit, non-empty ``unverified_merge_reason`` returns False.
    """
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return True
    reason = review_loop.get(UNVERIFIED_MERGE_REASON_KEY)
    return not (isinstance(reason, str) and reason)


# Durable signal the no-CI merge monitor stamps onto ``review_loop`` when it
# independently ran ONLY the declared acceptance test as a targeted run and it
# exited green (see ``orchestrator_ci_monitor`` and
# ``ci_resolution.acceptance_test_is_green``). Read defensively so a task without
# it (every legacy / older-agent merge) behaves exactly as today.
ACCEPTANCE_TEST_TARGETED_GREEN_KEY = "acceptance_test_targeted_green"


def _acceptance_test_targeted_green(task: Task) -> bool:
    """Whether the merge gate independently ran the declared acceptance test as a
    TARGETED run and it exited GREEN (fail-safe / defensive).

    Reads ``review_loop['acceptance_test_targeted_green']`` — the durable signal
    the no-CI monitor stamps when :func:`ci_resolution.acceptance_test_is_green`
    held. Returns True ONLY on a literal ``True`` value; a missing / malformed /
    non-dict review_loop, or an older agent that never emitted the signal, yields
    False so today's behavior is preserved.
    """
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return False
    return review_loop.get(ACCEPTANCE_TEST_TARGETED_GREEN_KEY) is True


def _merge_acceptance_test_block(task: Task) -> dict | None:
    """Read the declared acceptance test's merge-test verdict block, defensively.

    Forward-compatible read: when a verdict-shaped dict carrying a normalized
    ``acceptanceTest`` block is present on the task's ``review_loop``, return the
    block; else return ``None``. The no-CI monitor currently distills the block
    into the durable ``acceptance_test_targeted_green`` /
    ``acceptance_test_targeted_descriptor`` signals (see ``orchestrator_ci_monitor``)
    rather than persisting the block itself, so this degrades to ``None`` for
    every legacy / older-agent task — every caller MUST fall back to
    :func:`_acceptance_test_targeted_green`.
    """
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return None
    verdict = review_loop.get("verify_tests_verdict")
    if not isinstance(verdict, dict):
        return None
    block = verdict.get("acceptanceTest")
    return block if isinstance(block, dict) else None


def _acceptance_test_exists_at_merge(task: Task) -> bool:
    """Whether the declared acceptance test is confirmed to EXIST in the repo at
    the merge commit — independent of whether the PR's diff touches it.

    True when the merge gate physically executed the declared test as a targeted
    run (``acceptance_test_targeted_green`` — a test that ran must exist), or a
    merge-test verdict block records the test as ``present``. Defensive: absent /
    malformed evidence yields False, so a name-dropped test that can be shown to
    exist nowhere is never credited.
    """
    if _acceptance_test_targeted_green(task):
        return True
    block = _merge_acceptance_test_block(task)
    return bool(block and block.get("present") is True)


def _acceptance_test_ran_green_at_merge(task: Task) -> bool:
    """Whether the declared acceptance test is confirmed to have RAN GREEN under a
    real merge-test verdict — independent of the PR's diff.

    True when the targeted-green signal was stamped (the merge gate independently
    executed ONLY the declared test and it exited green), or a merge-test verdict
    block records the declared test as present, ran, with status ``"ran"`` and
    ``passed``. A test that exists but did not run green (skipped / absent /
    failed) yields False.
    """
    if _acceptance_test_targeted_green(task):
        return True
    block = _merge_acceptance_test_block(task)
    return bool(
        block
        and block.get("present") is True
        and block.get("ran") is True
        and block.get("status") == "ran"
        and block.get("passed") is True
    )


def _evaluate_named_test(
    evidence: AcceptanceEvidence,
    changed: list[str],
    verified: bool,
    truncated: bool = False,
    parent_derived: bool = False,
    targeted_green: bool = False,
    test_exists: bool = False,
    ran_green: bool = False,
) -> AcceptanceGateVerdict:
    """Fail-closed gate for a ``named_test`` acceptance declaration.

    Passes only when the named test (1) is among the PR's changed files — it was
    actually added/modified by this PR rather than merely claimed — OR is
    confirmed to EXIST in the repo at the merge commit with a green merge-test
    run (see ``test_exists`` / ``ran_green``), (2) ran green under a real
    merge-test verdict, and (3) the PR also ships a NON-test production change
    for the test to exercise (the changed seam). A test-only diff fails: a test
    that ships no changed production code cannot be exercising the changed seam,
    and an always-true assertion added in isolation is exactly that shape.
    Trivial / placeholder test references are already rejected fail-closed at
    declaration time by ``AcceptanceEvidence`` validation.

    The presence-in-diff check is the FIRST anti-fraud guard and stays intact: a
    declared test that is neither in the diff NOR confirmed to exist at the merge
    commit is a name-drop and hard-fails. ``test_exists`` (confirmed existence at
    the merge commit) REPLACES presence-in-diff as the anti-fraud guard for the
    behaviour-preserving-refactor shape: a pre-existing test that the PR re-verifies
    without modifying is honest machine-checked evidence when the merge gate
    physically ran it green. ``ran_green`` confirms the SPECIFIC declared test ran
    green under a real merge-test verdict — for a test the PR did NOT modify, the
    full-suite ``verified`` flag is not per-test evidence, so the existence path
    additionally requires ``ran_green`` (or ``targeted_green``).

    ``targeted_green`` is the ONE relaxation of requirement (2): when the
    full-suite merge fell open UNVERIFIED (``verified is False``) but the merge
    gate INDEPENDENTLY executed the SPECIFIC declared acceptance test as a
    TARGETED run and it exited green (stamped by the no-CI monitor onto
    ``review_loop['acceptance_test_targeted_green']`` when
    :func:`ci_resolution.acceptance_test_is_green` held), the gate does NOT fail
    closed at the verification step — the targeted green run is honest
    machine-checked proof of the exact claim, so it continues to the proven
    branch and mints ``proven=True``. This does NOT require running the whole
    suite. ``ran_green`` is the same proof read directly from a merge-test verdict
    block when one is persisted (forward-compatible; see
    :func:`_acceptance_test_ran_green_at_merge`). When ``verified``,
    ``targeted_green``, and ``ran_green`` are ALL False the gate keeps today's
    fail-closed outcome. Neither relaxation ever overrides the parent-derived soft
    cap or the truncation / github-verified handling — those stay as-is (a
    parent-derived match is still capped at satisfied/unproven even when the
    declared test ran green, because the match may live in a sibling's slice of
    the combined diff).

    ``truncated`` marks that the changed-file list was capped (a >50-file PR): the
    declared test may have shipped beyond the truncation point, so its absence
    from ``changed`` is indeterminate rather than proof it was never added.
    Recording ``satisfied=False`` there would penalize a large PR for a data
    artifact and block its merge from crediting as success, so instead the merge
    is credited soft (``satisfied=True, proven=False``) — the specific test could
    not be machine-verified from the truncated input.

    Absence on a truncated list is ALWAYS inconclusive, even when the file list
    was sourced from GitHub's API rather than an agent self-report (the
    ``_pr_changed_files_github_verified`` sentinel). Our own
    :func:`stamp_changed_files_signal` caps the stored list at 50 paths
    *regardless of source*, so a GitHub-verified PR with more than 50 changed
    files still drops every path beyond index 50. A legitimately declared test
    lying beyond that stored prefix would read as absent and unsoundly hard-fail a
    truthful declaration. So truncation always yields the soft (inconclusive)
    credit above; nothing overrides it. Where the list is complete
    (``truncated=False``), the plain hard-fail below already applies and is
    genuinely authoritative because every changed path is stored.

    ``parent_derived`` marks that the changed-file list was INHERITED from a
    rollup parent's combined diff (the union of all siblings' changes) rather than
    the child's own PR. Because a child's declared test could physically live in a
    sibling's slice of that union, a parent-derived match is real enough to clear
    ``declared_unsatisfied`` (satisfied) but must NOT mint machine ``proven``
    credit — the final proof branch is capped at ``satisfied=True, proven=False``.
    """
    declared_path = (evidence.test_path or "").partition("::")[0].strip()
    test_present = any(_paths_match(cf, declared_path) for cf in changed)
    if not test_present:
        if truncated:
            return AcceptanceGateVerdict(
                True,
                True,
                evidence.kind,
                f"declared named_test {declared_path!r} could not be confirmed "
                "among the PR's changed files because the changed-file list was "
                "truncated (>50 files, only the first 50 paths are stored) — the "
                "test may ship beyond the stored prefix, so absence is "
                "inconclusive; crediting soft without machine proof",
            )
        # A declared test the PR did NOT modify can still be PROVEN when it EXISTS
        # at the merge commit and RAN GREEN: a behaviour-preserving refactor that
        # re-verifies a pre-existing test (rather than adding a new one) is honest
        # machine-checked evidence even though the test is absent from the diff.
        # Existence-at-merge replaces presence-in-diff as the anti-fraud guard — a
        # test the merge gate physically executed green cannot be a fabricated
        # name-drop. Absent existence evidence keeps the hard fail below, so
        # name-dropping a test that exists nowhere is never credited.
        if not test_exists:
            return AcceptanceGateVerdict(
                True,
                False,
                evidence.kind,
                f"declared named_test {declared_path!r} is not among the PR's "
                "changed files — the claimed test was not added or modified here",
            )
        # The test exists at the merge commit. For a test the PR did NOT modify,
        # the full-suite ``verified`` flag is not per-test evidence — only a green
        # run recorded for THIS test (targeted execution or a merge-test verdict
        # block) proves it ran green, so the existence path requires it explicitly.
        if not targeted_green and not ran_green:
            return AcceptanceGateVerdict(
                True,
                False,
                evidence.kind,
                f"declared named_test {declared_path!r} did not run green under a "
                "real merge-test verdict (the merge fell open without verification)",
            )
    if not verified and not targeted_green and not ran_green:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} did not run green under a "
            "real merge-test verdict (the merge fell open without verification)",
        )
    # When ``verified`` is False but ``targeted_green``/``ran_green`` is True, the
    # merge gate ran ONLY the declared test as a targeted run and it exited green —
    # honest machine-checked proof of the exact claim, so we continue past the
    # verification step to the proven branch below rather than failing closed.
    via_targeted = (targeted_green or ran_green) and not verified
    seam_files = [cf for cf in changed if not _is_test_path(cf)]
    if not seam_files:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} ships no production code "
            "change it could exercise (the diff is test-only)",
        )
    if not _test_semantically_grounded(declared_path, seam_files):
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} exercises a production seam "
            "that does not semantically correspond to the declared test module — "
            "none of the changed production files match the test file's module name",
        )
    if parent_derived:
        # The changed-file list was inherited from a rollup parent's combined
        # diff (a superset union of every sibling's changes). The declared test
        # matches SOMEWHERE in that union, but it could be a sibling's slice, so
        # this cannot be a machine proof. Credit soft: clears declared_unsatisfied
        # without minting proven credit.
        return AcceptanceGateVerdict(
            True,
            True,
            evidence.kind,
            f"declared named_test {declared_path!r} matches the rollup parent's "
            "combined changed-file diff (inherited, not the child's own PR) — "
            "credited soft without machine proof, as the match may live in a "
            "sibling's slice of the union diff",
        )
    if via_targeted:
        if test_present:
            reason = (
                f"named_test {declared_path!r} was added/modified and ran GREEN via "
                f"a TARGETED execution of the declared test (the full-suite merge "
                f"fell open unverified), and exercises a semantically grounded "
                f"production seam ({len(seam_files)} non-test file(s))"
            )
        else:
            reason = (
                f"named_test {declared_path!r} was NOT modified by this PR but "
                f"EXISTS in the repo at the merge commit and ran GREEN via a "
                f"TARGETED execution of the declared test, and exercises a "
                f"semantically grounded production seam ({len(seam_files)} non-test "
                f"file(s))"
            )
    else:
        if test_present:
            reason = (
                f"named_test {declared_path!r} was added/modified, ran green under "
                f"a verified merge, and exercises a semantically grounded production "
                f"seam ({len(seam_files)} non-test file(s))"
            )
        else:
            reason = (
                f"named_test {declared_path!r} was NOT modified by this PR but "
                f"EXISTS in the repo at the merge commit, ran green under a "
                f"verified merge, and exercises a semantically grounded production "
                f"seam ({len(seam_files)} non-test file(s))"
            )
    return AcceptanceGateVerdict(
        True,
        True,
        evidence.kind,
        reason,
        proven=True,
        proof_ref=evidence.test_path or declared_path or None,
    )


def _evaluate_named_test_premerge(
    evidence: AcceptanceEvidence,
    changed: list[str],
    truncated: bool = False,
    parent_derived: bool = False,
) -> AcceptanceGateVerdict:
    """Pre-merge structural gate for a ``named_test`` acceptance declaration.

    Mirrors :func:`_evaluate_named_test`'s structurally-checkable requirements
    (test present, non-test seam, semantic grounding) while skipping the
    merge-time green-run requirement. Same fail-closed reason strings, so
    :func:`classify_unsatisfied_reason` maps them identically. Truncated /
    parent-derived input stays inconclusive (satisfied/unproven), as at merge.
    """
    declared_path = (evidence.test_path or "").partition("::")[0].strip()
    test_present = any(_paths_match(cf, declared_path) for cf in changed)
    if not test_present:
        if truncated:
            return AcceptanceGateVerdict(
                True,
                True,
                evidence.kind,
                f"declared named_test {declared_path!r} could not be confirmed "
                "among the PR's changed files because the changed-file list was "
                "truncated (>50 files, only the first 50 paths are stored) — the "
                "test may ship beyond the stored prefix, so absence is "
                "inconclusive before merge",
            )
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} is not among the PR's "
            "changed files — the claimed test was not added or modified here",
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
    if not _test_semantically_grounded(declared_path, seam_files):
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} exercises a production seam "
            "that does not semantically correspond to the declared test module — "
            "none of the changed production files match the test file's module name",
        )
    if parent_derived:
        return AcceptanceGateVerdict(
            True,
            True,
            evidence.kind,
            f"declared named_test {declared_path!r} matches the rollup parent's "
            "combined changed-file diff (inherited, not the child's own PR) — "
            "credited soft without machine proof before merge",
        )
    return AcceptanceGateVerdict(
        True,
        True,
        evidence.kind,
        f"declared named_test {declared_path!r} is present among the PR's changed "
        f"files and exercises a semantically grounded production seam "
        f"({len(seam_files)} non-test file(s)); the green run is verified at merge",
    )


def _artifact_tokens(artifact_id: str) -> list[str]:
    """Substantive lowercase tokens of an ``artifact_id``, split on non-alphanums.

    ``release-deploy-artifact-registry`` → ``["release", "deploy", "registry"]``.
    Single-character and generic structural tokens (``artifact``) carry no
    grounding signal — a changed path almost always contains them — so they are
    dropped, leaving only tokens that meaningfully tie the declaration to a file.
    """
    raw = [t for t in re.split(r"[^a-z0-9]+", artifact_id.lower()) if t]
    generic = {"artifact", "artifacts"}
    return [t for t in raw if len(t) > 1 and t not in generic]


def _artifact_grounded(artifact_id: str, changed: list[str]) -> bool:
    """Whether a changed-file path reflects the declared ``artifact_id``.

    Grounds the declaration in what actually shipped: at least one substantive
    token of the artifact_id must appear as a token within a changed-file path
    (path split on non-alphanumerics, so ``release_promotion.py`` yields
    ``release``/``promotion``/``py``). Mirrors ``_paths_match``'s tolerance for
    differing path/slug conventions without requiring an exact basename match.
    """
    tokens = _artifact_tokens(artifact_id)
    if not tokens:
        return False
    for path in changed:
        path_tokens = {t for t in re.split(r"[^a-z0-9]+", path.lower()) if t}
        if any(t in path_tokens for t in tokens):
            return True
    return False


def _evaluate_downstream_artifact(
    evidence: AcceptanceEvidence, changed: list[str]
) -> AcceptanceGateVerdict:
    """Grounded gate for a ``downstream_artifact`` acceptance declaration.

    Unlike ``observable_behavior`` (which only requires a non-empty diff), a
    ``downstream_artifact`` names a concrete ``artifact_id`` a later task
    consumes. Satisfied only when that declared artifact_id is reflected in the
    shipped changed files (a substantive token of the slug appears in a changed
    path) — so a meaningful-sounding artifact_id paired with a completely
    unrelated diff fails closed. An empty diff shipped nothing to ground against
    and also fails closed. Satisfied but never machine-checked (``proven=False``):
    a path-token match shows the artifact was plausibly delivered, not that a
    downstream consumer actually exercised it.
    """
    if not changed:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            "declared downstream_artifact acceptance evidence but the task "
            "shipped no changed files to deliver it",
        )
    artifact_id = (evidence.artifact_id or "").strip()
    if not _artifact_grounded(artifact_id, changed):
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared downstream_artifact {artifact_id!r} is not reflected in "
            f"any of the {len(changed)} shipped changed file(s) — the diff does "
            "not deliver the named artifact",
        )
    return AcceptanceGateVerdict(
        True,
        True,
        evidence.kind,
        f"downstream_artifact {artifact_id!r} is reflected in the shipped "
        "changed files",
        proof_ref=artifact_id or None,
    )


def _evaluate_visual(task: Task, evidence: AcceptanceEvidence) -> AcceptanceGateVerdict:
    """Fail-closed gate for a ``visual`` acceptance declaration.

    A visual declaration is satisfied only when the task context already
    carries the uploaded viewport screenshot URI for the declared
    ``(component, viewport_width)`` pair.  The shared resolver keeps the object
    name derivation consistent with the upload/download path and fails closed
    on absent or mismatched evidence.
    """
    if evidence.component is None or evidence.viewport_width is None:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            "declared visual acceptance evidence is malformed or incomplete",
        )

    resolved = resolve_visual_evidence(
        task.project_id,
        evidence.component,
        evidence.viewport_width,
        task.context,
    )
    if not resolved["resolved"]:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            "declared visual acceptance evidence but no matching viewport "
            f"screenshot was uploaded for component {evidence.component!r} at "
            f"{evidence.viewport_width}px",
        )
    return AcceptanceGateVerdict(
        True,
        True,
        evidence.kind,
        "visual acceptance evidence matches the uploaded viewport screenshot "
        f"{resolved['object_name']}",
        proven=True,
        proof_ref=f"{evidence.component}@{evidence.viewport_width}",
    )


def _evaluate_coverage_delta(
    evidence: AcceptanceEvidence,
    context: Mapping[str, Any] | None,
) -> AcceptanceGateVerdict:
    """Fail-closed gate for a ``coverage_delta`` acceptance declaration.

    Reads the coverage measurement stored in task context (``TEST_COVERAGE_RESULT_KEY``)
    by the verify-tests handler and compares the achieved ``coverage_pct`` against
    the declared threshold.  Passes only when the coverage result is present and
    carries a numeric ``coverage_pct`` at or above the threshold.

    Note: Unlike :func:`_evaluate_named_test`, this function does **not** receive
    the ``verified`` flag.  The coverage measurement itself is produced only by a
    real test run, so an absent result implicitly means no test ran — no separate
    verification gate is needed.

    The result is marked ``proven=True`` only when the ``coverage_pct`` comes
    from genuine line/statement coverage instrumentation (pytest-cov, c8), NOT
    from the test-pass-rate fallback.  When the orchestrator stores a pass-rate
    proxy (``source="pass_rate"``) because no instrumentation was active, the
    verdict is ``satisfied=True`` but ``proven=False`` — pass-rate is semantically
    distinct from changed-line coverage.

    Absent / non-numeric result, absent or non-numeric threshold, or coverage below
    threshold all fail closed.
    """
    threshold = coerce_number(evidence.threshold)
    if threshold is None:
        return AcceptanceGateVerdict(
            True, False, evidence.kind,
            "coverage_delta acceptance evidence requires a numeric threshold "
            "(coverage percentage 0–100)",
        )

    result = (context or {}).get(TEST_COVERAGE_RESULT_KEY)
    if not isinstance(result, dict):
        return AcceptanceGateVerdict(
            True, False, evidence.kind,
            "coverage_delta acceptance evidence requires a test coverage result "
            f"in task context ({TEST_COVERAGE_RESULT_KEY}) — no result found",
        )

    coverage_pct = coerce_number(result.get("coverage_pct"))
    if coverage_pct is None:
        return AcceptanceGateVerdict(
            True, False, evidence.kind,
            "coverage_delta acceptance evidence: coverage result is missing "
            "numeric coverage_pct",
        )

    # Distinguish instrumented line coverage from the test-pass-rate fallback.
    # When the orchestrator stored source="pass_rate", the value represents
    # the fraction of tests that passed, NOT line coverage — the claim can be
    # satisfied (a real test run happened) but is NOT machine-checked proof
    # of changed-line coverage.
    is_pass_rate = result.get("source") == "pass_rate"

    if criterion_met(coverage_pct, ">=", threshold):
        if is_pass_rate:
            reason = (
                f"coverage_delta acceptance evidence: achieved {coverage_pct}% "
                f"test pass rate >= {threshold}% threshold (pass-rate proxy — "
                f"no line-coverage instrumentation was active)"
            )
        else:
            reason = (
                f"coverage_delta acceptance evidence: achieved {coverage_pct}% "
                f"coverage >= {threshold}% threshold"
            )
        return AcceptanceGateVerdict(
            True, True, evidence.kind,
            reason,
            proven=not is_pass_rate,
            proof_ref=(
                None if is_pass_rate
                else f"coverage_delta={coverage_pct}%"
            ),
        )

    return AcceptanceGateVerdict(
        True, False, evidence.kind,
        f"coverage_delta acceptance evidence: achieved {coverage_pct}% "
        f"coverage < {threshold}% threshold",
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
    truncated = bool((task.context or {}).get("_pr_changed_files_truncated"))
    parent_derived_files = bool(
        (task.context or {}).get(PARENT_DERIVED_CHANGED_FILES_KEY)
    )
    targeted_green = _acceptance_test_targeted_green(task)
    test_exists = _acceptance_test_exists_at_merge(task)
    ran_green = _acceptance_test_ran_green_at_merge(task)

    if evidence.kind == "named_test":
        return _evaluate_named_test(
            evidence,
            changed,
            verified,
            truncated,
            parent_derived=parent_derived_files,
            targeted_green=targeted_green,
            test_exists=test_exists,
            ran_green=ran_green,
        )

    if evidence.kind == "downstream_artifact":
        return _evaluate_downstream_artifact(evidence, changed)

    if evidence.kind == "visual":
        return _evaluate_visual(task, evidence)

    if evidence.kind == "coverage_delta":
        return _evaluate_coverage_delta(evidence, task.context)

    # observable_behavior carries no machine-checkable assertion the backend can
    # independently re-run at completion. To the extent checkable, require that
    # the task actually shipped a real diff (a non-empty changed-file set) — a
    # declared behavior with an empty diff shipped nothing to exercise, so it
    # fails closed. The declaration's substance (non-trivial description) is
    # already enforced at validation.
    if evidence.kind == "observable_behavior":
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


def evaluate_premerge_acceptance(task: Task) -> AcceptanceGateVerdict | None:
    """Evaluate the structurally-checkable half of ``task``'s declared acceptance
    evidence while the PR is still open.

    Mirrors :func:`evaluate_code_acceptance` but only judges what is decidable
    from the declaration + the PR's changed files before merge: named_test is
    present with a non-test, semantically-grounded seam (the green run is
    verified at merge); downstream_artifact is delivered by the diff; visual /
    coverage_delta declarations are guaranteed well-formed by the schema (the
    screenshot / measurement is verified at merge); observable_behavior ships a
    real diff. A structural defect returns ``satisfied=False`` with the same
    actionable reason string the merge-time gate uses
    (:func:`classify_unsatisfied_reason` maps it identically); a task with no
    declaration returns ``None``.
    """
    evidence = read_acceptance_evidence(task.first_payload)
    if evidence is None:
        return None

    changed = _changed_files(task)
    truncated = bool((task.context or {}).get("_pr_changed_files_truncated"))
    parent_derived_files = bool(
        (task.context or {}).get(PARENT_DERIVED_CHANGED_FILES_KEY)
    )

    if evidence.kind == "named_test":
        return _evaluate_named_test_premerge(
            evidence,
            changed,
            truncated,
            parent_derived=parent_derived_files,
        )

    if evidence.kind == "downstream_artifact":
        # Purely structural: grounded against the changed files. Reused as-is.
        return _evaluate_downstream_artifact(evidence, changed)

    if evidence.kind == "visual":
        # Schema guarantees component + viewport_width; the screenshot match is
        # a merge-time check (see _evaluate_visual).
        return AcceptanceGateVerdict(
            True, True, evidence.kind,
            "declared visual acceptance evidence is well-formed (component + "
            "viewport width); the uploaded screenshot is verified at merge",
        )
    if evidence.kind == "coverage_delta":
        # Schema guarantees a numeric threshold; the measurement is a merge-time
        # check (see _evaluate_coverage_delta).
        return AcceptanceGateVerdict(
            True, True, evidence.kind,
            "coverage_delta acceptance evidence declares a numeric threshold; the "
            "coverage measurement is verified at merge",
        )

    if evidence.kind == "observable_behavior":
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
    scoring unchanged. Otherwise records ``{satisfied, proven, kind, reason}``
    under :data:`CODE_ACCEPTANCE_VERDICT_KEY` so later scoring
    (:func:`acceptance_blocks_success`, :func:`acceptance_is_proven`) can consume
    it. The caller is responsible for committing.
    """
    verdict = evaluate_code_acceptance(task)
    if not verdict.declared:
        return None
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": verdict.satisfied,
        "proven": verdict.proven,
        "kind": verdict.kind,
        "reason": verdict.reason,
        "proof_ref": verdict.proof_ref,
    }
    # A re-recorded verdict means new provenance arrived (parent-derived changed
    # files, a GitHub-verified diff, etc.) AFTER the one-shot backfill sweep had
    # already examined this task. Clear the marker so the task re-enters the
    # sweep pool and can earn criterion evidence if it flips to proven — or a
    # soft satisfied flip in the missing-from-diff class. Absent on first
    # recording; popping a missing key is a no-op.
    ctx.pop("_backfill_checked_at", None)
    ctx.pop(BACKFILL_MISSING_DIFF_TERMINAL_KEY, None)
    task.context = ctx
    return verdict


def premerge_acceptance_finding(task: Task) -> dict | None:
    """Read the durable pre-merge acceptance finding stamped on ``task`` (``None``
    when absent / malformed). Pure and side-effect-free — the channel the
    review/fix pipeline reads to surface a structural defect to the fix agent.
    """
    finding = (task.context or {}).get(PREMERGE_ACCEPTANCE_FINDING_KEY)
    return finding if isinstance(finding, dict) else None


def stamp_premerge_acceptance_finding(task: Task) -> dict | None:
    """Judge the structurally-checkable half of the declared acceptance evidence
    while the PR is open and route a failure into the review/fix loop.

    Stamps a durable finding under :data:`PREMERGE_ACCEPTANCE_FINDING_KEY` (a
    top-level key — never inside ``review_loop``, so merge-verification
    semantics are untouched) when a structural defect exists; else clears any
    stale finding and returns ``None``, so a task that corrected its code or
    declaration self-heals on the next status POST. Mutates ``task.context``
    (the caller commits). Fail-safe: never raises.
    """
    verdict = evaluate_premerge_acceptance(task)
    ctx = dict(task.context or {})
    if verdict is None or verdict.satisfied:
        if PREMERGE_ACCEPTANCE_FINDING_KEY in ctx:
            ctx.pop(PREMERGE_ACCEPTANCE_FINDING_KEY)
            task.context = ctx
        return None
    finding = {
        "satisfied": False,
        "kind": verdict.kind,
        "cause": classify_unsatisfied_reason(
            {"kind": verdict.kind, "reason": verdict.reason, "satisfied": False}
        ),
        "reason": verdict.reason,
        "evaluated_at": _utcnow().isoformat(),
    }
    ctx[PREMERGE_ACCEPTANCE_FINDING_KEY] = finding
    task.context = ctx
    return finding


def format_premerge_acceptance_finding(finding: Any) -> str:
    """Render a pre-merge acceptance finding as a fix-prompt block, ``""`` for a
    missing / malformed / non-blocking finding so callers prepend the result
    unconditionally. Naming the structural defect up front lets the fix agent
    correct code or declaration in the same pass as the review feedback.
    """
    if not isinstance(finding, dict) or finding.get("satisfied") is not False:
        return ""
    reason = str(finding.get("reason") or "").strip()
    if not reason:
        return ""
    cause = str(finding.get("cause") or "").strip()
    block = [
        "PRE-MERGE ACCEPTANCE FAILURE — this PR's declared acceptance evidence "
        "is structurally unsatisfiable. Fix the CODE or the DECLARATION so the "
        "declared evidence is genuinely delivered by this PR before it merges:",
        "",
        f"- {reason}",
    ]
    if cause:
        block.append(f"  (cause: {cause})")
    block.append("")
    return "\n".join(block)


def acceptance_blocks_success(recorded: Any) -> bool:
    """Whether a recorded acceptance verdict blocks crediting a merge as success.

    ``True`` only when a verdict was recorded AND it is explicitly unsatisfied.
    Absence of the key (``None``) — the case for every task that declared no
    acceptance evidence, plus all legacy rows — returns ``False`` so those tasks
    keep their current bare-merge success. Additive and backward-compatible,
    mirroring the now-removed ``reproduction_blocks_success`` predicate.
    """
    if not isinstance(recorded, dict):
        return False
    return recorded.get("satisfied") is False


def acceptance_was_declared(recorded: Any) -> bool:
    """Whether a task DECLARED acceptance evidence (any kind) at plan time.

    ``True`` exactly when a verdict dict was recorded — the verdict is stamped
    only for tasks that declared evidence (see
    :func:`record_code_acceptance_verdict`, which no-ops on the undeclared case),
    so the presence of the key is itself the declaration signal. Undeclared and
    legacy rows (no key, ``None``, or any non-dict) return ``False``. Feeds the
    additive declaration-coverage aggregate without touching any existing count.
    """
    return isinstance(recorded, dict)


def acceptance_is_proven(recorded: Any) -> bool:
    """Whether a recorded verdict is machine-checked PROVEN, not merely delivered.

    ``True`` only when a verdict was recorded AND it carries ``proven is True``.
    Two evidence paths produce ``proven=True``:

    1. A satisfied ``named_test`` that ran green under a real merge-test verdict
       and exercises a changed production seam (machine-checked proof).
    2. A satisfied ``downstream_artifact`` that has been consumed — i.e.
       ``record_artifact_consumptions_for_task`` ran and upgraded the verdict to
       ``proven=True, consumed=True`` (recorded downstream-artifact consumption).
       ``consumed`` is a strict subset: every consumed verdict is proven, but a
       satisfied-but-unconsumed ``downstream_artifact`` is ``proven=False``.

    A satisfied ``observable_behavior`` (soft / asserted by a shipped diff only),
    every unsatisfied / fail-closed case, and all undeclared or legacy rows return
    ``False``. This is the additive TRUE proven signal, strictly distinct from
    ``verified_prs_merged`` (which still credits soft and undeclared merges).
    Tolerant of legacy verdict dicts written before the ``proven`` key existed —
    a missing key reads as ``False``, never proven.
    """
    if not isinstance(recorded, dict):
        return False
    return recorded.get("proven") is True


def _context_merge_verification_status(context: Mapping[str, Any] | None) -> str:
    """Return the merge-verification bucket derivable from a task context dict."""
    review_loop = (context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return "verified"
    reason = review_loop.get(UNVERIFIED_MERGE_REASON_KEY)
    if not isinstance(reason, str) or not reason:
        return "verified"
    if is_degraded_unverified_reason(reason):
        return "unverified_degraded"
    return "unverified_benign"


def classify_merged_acceptance_context(context: Mapping[str, Any] | None) -> str:
    """Classify one landed task context into the canonical acceptance buckets."""
    if _context_merge_verification_status(context) == "unverified_degraded":
        return "merged_unverified_degraded"
    recorded = (context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    if not isinstance(recorded, dict):
        # No explicit declaration. Infer verified test co-change from the signals
        # the merge already carries: a CI-VERIFIED merge (no fall-open reason)
        # whose PR changed at least one test file AND at least one non-test
        # production file is stronger evidence than a bare undeclared merge — CI
        # genuinely gated the change and tests shipped alongside it. This mirrors
        # the named_test gate's (2)+(3) conditions (verified merge + a production
        # seam for the test to exercise) minus the declaration linking the test
        # to the claim, so it is deliberately a distinct, weaker bucket than
        # satisfied_unproven. A benign/degraded fall-open (CI did not actually
        # run) does NOT qualify — test presence is moot without verification.
        if _context_merge_verification_status(context) == "verified":
            ctx = context or {}
            changed = ctx.get("_pr_changed_files")
            if isinstance(changed, list):
                # Prefer the non-lossy full-list summary (stamped by
                # stamp_changed_files_signal) so a >50-file PR whose test or
                # production paths fall beyond the truncation point is not
                # silently demoted to "undeclared". Legacy rows lack the summary
                # and fall back to scanning the (possibly truncated) list.
                has_test = ctx.get("_pr_changed_files_has_test")
                has_prod = ctx.get("_pr_changed_files_has_prod")
                if not isinstance(has_test, bool) or not isinstance(has_prod, bool):
                    str_paths = [
                        p for p in changed if isinstance(p, str) and p.strip()
                    ]
                    has_test = any(_is_test_path(p) for p in str_paths)
                    has_prod = any(not _is_test_path(p) for p in str_paths)
                if has_test and has_prod:
                    return "inferred_test_cochange"
        return "undeclared"
    if acceptance_blocks_success(recorded):
        return "declared_unsatisfied"
    if recorded.get("satisfied") is True:
        if acceptance_is_proven(recorded):
            return "proven"
        return "satisfied_unproven"
    return "malformed"


# The three mutually-exclusive reasons an already-``undeclared`` merge stayed
# undeclared, partitioning the ``undeclared`` bucket for operator-visible audit.
_UNDECLARED_REASON_KEYS: tuple[str, ...] = (
    "not_ci_verified",
    "no_changed_files_context",
    "no_test_prod_cochange",
)


def classify_undeclared_reason(context: Mapping[str, Any] | None) -> str:
    """Sub-classify an ``undeclared`` merge context into WHY it stayed undeclared.

    Precondition: ``classify_merged_acceptance_context(context) == "undeclared"``.
    The three reasons mirror the undeclared branch of
    :func:`classify_merged_acceptance_context` EXACTLY, so the split partitions
    the undeclared bucket perfectly:

      * ``not_ci_verified`` — the merge was not CI-verified (benign fall-open),
        so test presence is moot and co-change can never be inferred.
      * ``no_changed_files_context`` — CI-verified, but no ``_pr_changed_files``
        list was recorded, so co-change cannot be inferred from the context.
      * ``no_test_prod_cochange`` — CI-verified with a changed-files list, but it
        did not co-change BOTH a test file and a non-test production file.

    Pure and read-only; never raises. On a context that is NOT actually
    undeclared the return value is meaningless (callers must gate on the bucket
    first), but the function still returns one of the three keys deterministically.
    """
    if _context_merge_verification_status(context) != "verified":
        return "not_ci_verified"
    changed = (context or {}).get("_pr_changed_files")
    if not isinstance(changed, list):
        return "no_changed_files_context"
    return "no_test_prod_cochange"


# The mutually-exclusive reasons an already-``declared_unsatisfied`` verdict
# failed, partitioning the ``declared_unsatisfied`` bucket for operator-visible
# audit. ``unknown`` is the fail-safe catch-all: it keeps tallies partitioning
# even on verdicts stamped without a ``reason`` (legacy / manually-written rows)
# or carrying a future reason string this module has not seen yet.
_UNSATISFIED_REASON_KEYS: tuple[str, ...] = (
    "named_test_missing_from_diff",
    "named_test_not_run_green",
    "named_test_test_only_diff",
    "named_test_ungrounded_seam",
    "artifact_empty_diff",
    "artifact_not_in_diff",
    "visual_malformed",
    "visual_no_screenshot",
    "coverage_missing_threshold",
    "coverage_missing_result",
    "coverage_below_threshold",
    "observable_empty_diff",
    "unrecognized_kind",
    "unknown",
)


def classify_unsatisfied_reason(recorded: Any) -> str:
    """Sub-classify a ``declared_unsatisfied`` verdict into WHY it failed.

    Precondition: ``acceptance_blocks_success(recorded) is True`` (a recorded
    verdict with ``satisfied is False``). Reads the persisted verdict dict —
    exactly what :func:`record_code_acceptance_verdict` stamps under
    :data:`CODE_ACCEPTANCE_VERDICT_KEY` — and returns one canonical cause keyed
    first on the verdict ``kind`` and disambiguated within the kind by a
    distinctive ``reason`` substring (every fail-closed reason string originates
    in this module). The returned keys are the causes an operator can act on:
    a named test that was never added to the diff, that fell open without a real
    green run, that shipped no production seam, or that is not semantically
    grounded; an artifact the diff never delivered; a malformed or missing
    visual screenshot; a coverage declaration with a missing threshold, a
    missing/invalid measurement, or a genuine below-threshold measurement; an
    observable-behavior declaration with an empty diff; an unrecognized
    declaration kind; and the ``unknown`` catch-all for verdicts whose reason
    was not recorded (legacy / manually-stamped rows).

    Pure and read-only; never raises. On a verdict that is NOT actually declared
    unsatisfied the return value is meaningless (callers must gate on the bucket
    first), but the function still returns one of the keys deterministically.
    """
    if not isinstance(recorded, dict):
        return "unknown"
    kind = recorded.get("kind")
    reason = recorded.get("reason")
    reason_str = reason if isinstance(reason, str) else ""

    # The unrecognized-kind reason is emitted regardless of the offending kind,
    # so it wins before the kind dispatch below.
    if "unrecognized acceptance kind" in reason_str:
        return "unrecognized_kind"

    if kind == "named_test":
        if "is not among the PR's changed files" in reason_str:
            return "named_test_missing_from_diff"
        if "did not run green under a real merge-test verdict" in reason_str:
            return "named_test_not_run_green"
        if "ships no production code change it could exercise" in reason_str:
            return "named_test_test_only_diff"
        if "does not semantically correspond to the declared test module" in reason_str:
            return "named_test_ungrounded_seam"
        return "unknown"
    if kind == "downstream_artifact":
        if "shipped no changed files to deliver it" in reason_str:
            return "artifact_empty_diff"
        if "is not reflected in" in reason_str:
            return "artifact_not_in_diff"
        return "unknown"
    if kind == "visual":
        if "malformed or incomplete" in reason_str:
            return "visual_malformed"
        if "no matching viewport screenshot was uploaded" in reason_str:
            return "visual_no_screenshot"
        return "unknown"
    if kind == "coverage_delta":
        if "requires a numeric threshold" in reason_str:
            return "coverage_missing_threshold"
        if "requires a test coverage result" in reason_str:
            return "coverage_missing_result"
        if "missing numeric coverage_pct" in reason_str:
            return "coverage_missing_result"
        if "coverage <" in reason_str:
            return "coverage_below_threshold"
        return "unknown"
    if kind == "observable_behavior":
        if "shipped no changed files to exercise it" in reason_str:
            return "observable_empty_diff"
        return "unknown"
    return "unknown"


def acceptance_counts_as_verified_context(
    context: Mapping[str, Any] | None,
) -> bool:
    """Whether a landed task context still counts as verified merged work.

    Verified merges include the strict proven, soft satisfied-unproven,
    inferred test-cochange, and undeclared buckets. Degraded, explicitly
    unsatisfied, and malformed rows do not count.
    """
    return classify_merged_acceptance_context(context) in {
        "proven",
        "satisfied_unproven",
        "inferred_test_cochange",
        "undeclared",
    }


def acceptance_is_consumed(recorded: Any) -> bool:
    """Whether a downstream_artifact verdict has been proven by consumption."""
    if not isinstance(recorded, dict):
        return False
    return recorded.get("consumed") is True


def _fallback_consumed_artifact_ids(task: Task) -> list[uuid.UUID]:
    """Delivered artifact prerequisites from direct dependencies for old agents."""
    depends_on = {str(dep) for dep in (task.depends_on or []) if dep}
    if not depends_on:
        return []
    raw = (task.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    if not isinstance(raw, list):
        return []
    ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_task_id") or "") not in depends_on:
            continue
        try:
            artifact_id = uuid.UUID(str(item.get("artifact_id") or ""))
        except (TypeError, ValueError):
            continue
        if artifact_id not in seen:
            seen.add(artifact_id)
            ids.append(artifact_id)
    return ids


def _authorized_consumed_artifact_ids(task: Task) -> set[uuid.UUID]:
    """Artifact ids explicitly delivered to this task and therefore reportable."""
    delivered = (task.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    if not isinstance(delivered, list):
        return set()
    allowed: set[uuid.UUID] = set()
    for item in delivered:
        if not isinstance(item, dict):
            continue
        try:
            artifact_id = uuid.UUID(str(item.get("artifact_id") or ""))
        except (TypeError, ValueError):
            continue
        allowed.add(artifact_id)
    return allowed


def _normalize_consumed_artifact_ids(
    task: Task,
    consumed_artifact_ids: Iterable[uuid.UUID | str] | None,
    *,
    modern_agent: bool = False,
) -> list[uuid.UUID]:
    if consumed_artifact_ids is None:
        if modern_agent:
            # Modern agent omitted the field entirely — treat as no consumption.
            # Do NOT fall back to delivered prerequisites; an omission from a
            # current-generation agent means the handler did not render artifact
            # prerequisites and therefore cannot have consumed any.
            return []
        raw = _fallback_consumed_artifact_ids(task)
    else:
        raw = list(consumed_artifact_ids)
    allowed = (
        _authorized_consumed_artifact_ids(task)
        if consumed_artifact_ids is not None
        else set()
    )
    ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for value in raw:
        try:
            artifact_id = (
                value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            )
        except (TypeError, ValueError):
            continue
        if consumed_artifact_ids is not None and artifact_id not in allowed:
            continue
        if artifact_id not in seen:
            seen.add(artifact_id)
            ids.append(artifact_id)
    return ids


async def record_artifact_consumptions_for_task(
    task: Task,
    session,
    consumed_artifact_ids: Iterable[uuid.UUID | str] | None = None,
    *,
    modern_agent: bool = False,
) -> list[ArtifactConsumption]:
    """Persist consumption rows and upgrade matching producer verdicts.

    Idempotent and additive: duplicate reports no-op, missing/legacy producer
    verdicts are left untouched, and existing proven/consumed verdicts are never
    downgraded.
    """
    ids = _normalize_consumed_artifact_ids(
        task, consumed_artifact_ids, modern_agent=modern_agent
    )
    if not ids:
        return []

    artifacts = (
        (await session.execute(select(Artifact).where(Artifact.id.in_(ids))))
        .scalars()
        .all()
    )
    artifacts_by_id = {artifact.id: artifact for artifact in artifacts}

    # Batch-load producers and existing consumptions once (was 2*N per-artifact
    # round-trips): a single Task query keyed by producer id and a single
    # ArtifactConsumption query for this consumer, looked up in-memory below.
    producer_task_ids = {artifact.task_id for artifact in artifacts}
    producers_by_id: dict[uuid.UUID, Task] = {}
    if producer_task_ids:
        producer_rows = (
            (await session.execute(select(Task).where(Task.id.in_(producer_task_ids))))
            .scalars()
            .all()
        )
        producers_by_id = {producer.id: producer for producer in producer_rows}
    existing_consumptions_by_artifact: dict[uuid.UUID, ArtifactConsumption] = {}
    existing_consumption_rows = (
        (
            await session.execute(
                select(ArtifactConsumption).where(
                    ArtifactConsumption.consumer_task_id == task.id,
                    ArtifactConsumption.artifact_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    for _consumption in existing_consumption_rows:
        existing_consumptions_by_artifact.setdefault(
            _consumption.artifact_id, _consumption
        )

    recorded: list[ArtifactConsumption] = []
    recorded_id_strings: list[str] = []

    for artifact_id in ids:
        artifact = artifacts_by_id.get(artifact_id)
        if artifact is None:
            continue
        producer = producers_by_id.get(artifact.task_id)
        if producer is None or producer.project_id != task.project_id:
            continue
        existing = existing_consumptions_by_artifact.get(artifact.id)
        if existing is None:
            existing = ArtifactConsumption(
                artifact_id=artifact.id,
                consumer_task_id=task.id,
                producer_task_id=artifact.task_id,
                logical_key=artifact.logical_key,
            )
            session.add(existing)
            await session.flush()
        recorded.append(existing)
        recorded_id_strings.append(str(artifact.id))

        declared_key = downstream_artifact_logical_key(producer)
        if not declared_key or artifact.logical_key != declared_key:
            continue
        producer_ctx = dict(producer.context or {})
        verdict = producer_ctx.get(CODE_ACCEPTANCE_VERDICT_KEY)
        if not (
            isinstance(verdict, dict)
            and verdict.get("kind") == "downstream_artifact"
            and verdict.get("satisfied") is True
        ):
            continue
        upgraded = dict(verdict)
        if upgraded.get("proven") is not True or upgraded.get("consumed") is not True:
            upgraded["proven"] = True
            upgraded["consumed"] = True
            upgraded["consumed_by_task_id"] = str(task.id)
            upgraded["consumed_artifact_id"] = str(artifact.id)
            producer_ctx[CODE_ACCEPTANCE_VERDICT_KEY] = upgraded
            producer.context = producer_ctx
            flag_modified(producer, "context")
            session.add(producer)
            try:
                from app.capability_proof import record_capability_proof

                producer_project = await session.get(Project, producer.project_id)
                if producer_project is not None:
                    record_capability_proof(producer, producer_project)
                    session.add(producer_project)
            except Exception:  # noqa: BLE001 — proof recording must never block consumption
                logger.warning(
                    "[ACCEPTANCE_GATE] capability proof recording failed for producer"
                    " task %s after downstream_artifact consumption",
                    getattr(producer, "id", None),
                    exc_info=True,
                )

    if recorded_id_strings:
        ctx = dict(task.context or {})
        prior = ctx.get("consumed_artifact_ids")
        merged = [str(v) for v in prior] if isinstance(prior, list) else []
        for artifact_id in recorded_id_strings:
            if artifact_id not in merged:
                merged.append(artifact_id)
        ctx["consumed_artifact_ids"] = merged
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)

    return recorded


def classify_merged_acceptance(task: Task) -> str:
    """Classify one merged task into the release acceptance buckets.

    Buckets are deliberately additive and fail-safe:
      * ``proven`` — non-degraded merge with strict machine-checked proof;
      * ``satisfied_unproven`` — non-degraded merge with soft satisfied
        declared acceptance;
      * ``inferred_test_cochange`` — non-degraded, CI-verified merge with no
        declaration whose PR co-changed a test file and a non-test production
        file (inferred evidence, weaker than a declared satisfied acceptance);
      * ``declared_unsatisfied`` — non-degraded merge with declared evidence
        that failed;
      * ``undeclared`` — non-degraded merge with no recorded declaration;
      * ``merged_unverified_degraded`` — merge whose verification was degraded;
      * ``malformed`` — non-degraded recorded verdict dict with no conclusive
        satisfied / unsatisfied value.

    Undeclared / legacy rows stay neutral by falling into ``undeclared``.
    """
    if merge_verification_status(task) == "unverified_degraded":
        return "merged_unverified_degraded"
    return classify_merged_acceptance_context(task.context or {})


def summarize_version_acceptance_aggregate(
    tasks: Iterable[Task],
) -> dict[str, Any]:
    """Summarize one version window's merged tasks into acceptance buckets."""
    aggregate: dict[str, Any] = {bucket: 0 for bucket in ACCEPTANCE_AGGREGATE_BUCKETS}
    aggregate["total"] = 0
    for task in tasks:
        if task.status != TaskStatus.COMPLETED:
            continue
        aggregate["total"] += 1
        aggregate[classify_merged_acceptance(task)] += 1
    aggregate["proven_rate"] = (
        round(aggregate["proven"] / aggregate["total"], 3)
        if aggregate["total"] > 0
        else None
    )
    satisfied = aggregate["proven"] + aggregate["satisfied_unproven"]
    aggregate["satisfied_rate"] = (
        round(satisfied / aggregate["total"], 3) if aggregate["total"] > 0 else None
    )
    return aggregate


# ---------------------------------------------------------------------------
# Backfill: false-negative acceptance verdicts (pre-fix)
# ---------------------------------------------------------------------------


async def backfill_acceptance_verdicts(session) -> int:
    """Re-evaluate pre-fix named_test acceptance verdicts that may have been
    false-negatives. Three historical failure classes are healed:

    1. ``satisfied=True, proven=False`` — recorded before the
       ``_CROSS_CUTTING_TEST_FILES`` semantic-grounding fix (8acdd3780,
       2026-07-09T09:23 UTC);
    2. ``satisfied=False`` with the "did not run green under a real
       merge-test verdict" reason — recorded while ``_merge_was_test_verified``
       treated an ABSENT/malformed ``review_loop`` as unverified, so a landed
       task whose context simply lacked a ``review_loop`` dict was scored
       unsatisfied even though the merge was genuinely verified;
    3. ``satisfied=False`` with the "is not among the PR's changed files"
       reason — recorded when changed-file provenance was absent or late
       (rollup children merge via the PARENT's single PR, so a verdict stamped
       before the parent's ``_pr_changed_files`` arrived reads as missing-from-
       diff). Late-arriving provenance flips these to satisfied (proven when
       the task's own PR files are GitHub-verified, else capped at
       satisfied_unproven for the parent-derived union diff). Candidates of
       this class are re-admitted to the sweep once changed-file provenance
       has arrived — even when an OLD run already stamped the one-shot
       ``_backfill_checked_at`` marker on them — and leave the window
       terminally (``_backfill_missing_diff_terminal_at``) when provenance
       was in hand yet re-evaluation still failed. Fresh provenance clears
       both markers, so late re-stamps always re-admit the task without the
       class ever monopolizing the LIMIT-200 window.

    Each class is re-evaluated with the current (fixed) logic, and
    :class:`~app.models.CriterionEvidence` is recorded via
    :func:`~app.acceptance_criteria.record_task_merge_evidence` when the new
    verdict is ``proven=True``.

    Bounded to 200 tasks per call so the reaper cycle is never blocked.
    Selection is ordered by ``completed_at`` ascending (oldest-first) so the
    LIMIT window advances deterministically across successive reaper cycles.
    Tasks that already have a proven ``CriterionEvidence`` row are excluded
    from the query to avoid re-scanning already-proven rows and to keep the
    window moving toward unprocessed candidates.

    Best-effort: individual evidence-recording failures are logged and
    skipped without aborting the remaining tasks.

    Returns the number of upgraded verdicts.
    """
    from datetime import datetime, timezone

    # Upper bound on completion time for re-evaluation candidates. The bound
    # tracks the current time so EVERY pre-fix verdict is eventually examined
    # exactly once — covering both failure classes above. Tasks completed after
    # the corrected predicates ship record correct verdicts at landing; the
    # ``_proven_exists`` and ``_backfill_checked`` filters keep the sweep
    # idempotent, so the window always advances.
    FIX_TIMESTAMP = datetime.now(timezone.utc)

    from app.database import resolve_dialect_name
    _dialect = resolve_dialect_name(session)
    if _dialect == "sqlite":
        from sqlalchemy import func
        # Use a portable JSON key-existence check: ``has_key`` (Postgres ``?``)
        # on Postgres, ``func.json_extract`` on SQLite, so the standalone smoke
        # gate (which runs against SQLite) does not fail.
        _backfill_checked = func.json_extract(
            Task.context, "$._backfill_checked_at"
        ).is_(None)
        # A portable nested-path extraction of the persisted verdict reason —
        # ``context -> 'code_acceptance_verdict' -> 'reason'`` on Postgres,
        # ``$.code_acceptance_verdict.reason`` on SQLite. NULL when the verdict
        # or reason is absent, so a LIKE on the result selects only the
        # missing-from-diff class.
        _missing_from_diff_verdict = func.json_extract(
            Task.context, "$.code_acceptance_verdict.reason"
        ).like("%is not among the PR's changed files%")
        # Re-admission gate for the missing-from-diff class: changed-file
        # provenance has arrived (``_pr_changed_files`` is always a list when
        # stamped, so NULL means absent) and no sweep has yet found the task
        # unprovable WITH that provenance in hand (terminal marker absent).
        _missing_diff_readmit = and_(
            _missing_from_diff_verdict,
            func.json_extract(
                Task.context, "$._pr_changed_files"
            ).is_not(None),
            func.json_extract(
                Task.context, "$." + BACKFILL_MISSING_DIFF_TERMINAL_KEY
            ).is_(None),
        )
    else:
        _backfill_checked = not_(Task.context.has_key("_backfill_checked_at"))
        _missing_from_diff_verdict = (
            Task.context["code_acceptance_verdict"]["reason"].astext.like(
                "%is not among the PR's changed files%"
            )
        )
        _missing_diff_readmit = and_(
            _missing_from_diff_verdict,
            Task.context.has_key("_pr_changed_files"),
            not_(Task.context.has_key(BACKFILL_MISSING_DIFF_TERMINAL_KEY)),
        )
    # Exclude tasks that already have a proven CriterionEvidence row from a
    # prior backfill run so the LIMIT window advances to unprocessed tasks.
    # The ``proven`` flag is read via ``detail ->> 'proven'`` on Postgres but
    # ``func.json_extract`` on SQLite (which returns 1 for a JSON ``true``) —
    # portable so the standalone smoke gate stays green.
    if _dialect == "sqlite":
        from sqlalchemy import func
        _proven_exists = exists(
            select(1).where(
                CriterionEvidence.source_kind == "task_merge",
                CriterionEvidence.source_id == cast(Task.id, String),
                func.json_extract(CriterionEvidence.detail, "$.proven") == 1,
            )
        ).correlate(Task)
    else:
        _proven_exists = (
            exists(
                select(1).where(
                    CriterionEvidence.source_kind == "task_merge",
                    CriterionEvidence.source_id == cast(Task.id, String),
                    CriterionEvidence.detail["proven"].astext == "true",
                )
            )
            .correlate(Task)
        )
    # Candidates are tasks the sweep has not examined yet, OR tasks whose
    # persisted verdict is the missing-from-diff class AND whose changed-file
    # provenance has since arrived — even if an OLD sweep run already stamped
    # the one-shot ``_backfill_checked_at`` marker on them (those markers were
    # stamped before the missing-from-diff class was re-evaluable, so honoring
    # them would leave the exact verdicts this sweep exists to heal permanently
    # frozen). The provenance-arrived condition is what keeps the class from
    # monopolizing the window: a task with NO provenance is examined once,
    # marked, and only re-admitted when provenance actually shows up; a task
    # whose provenance was present yet still unprovable earns the terminal
    # marker below and leaves the window for good (until fresh provenance
    # clears the markers and re-admits it).
    # Collect IDs only, then batch-fetch Task objects with a single
    # SELECT ... WHERE id IN (...) — eliminates up to 200 individual
    # session.get() calls (PERF finding M-P6). The initial ID-only query
    # is safe: if a mid-loop rollback inside record_task_merge_evidence
    # (which unconditionally expires ALL session objects on rollback)
    # occurs, the batch is re-fetched.
    result = await session.execute(
        select(Task.id).where(
            Task.status == TaskStatus.COMPLETED,
            Task.completed_at < FIX_TIMESTAMP,
            not_(_proven_exists),
            or_(_backfill_checked, _missing_diff_readmit),
        ).order_by(Task.completed_at.asc()).limit(200)
    )
    task_ids = list(result.scalars().all())

    # Batch-fetch all task objects before the loop — single
    # SELECT ... WHERE id IN (...) instead of up to 200 individual
    # session.get() calls. Pass task_ids directly (they are UUID objects
    # or strings depending on dialect; in_() handles both).
    # expire_on_commit=False prevents a commit inside record_task_merge_evidence
    # from expiring the freshly-fetched task object before its attributes are
    # read by that function.  A ROLLBACK still expires all session objects, so
    # we batch-fetch upfront and fall back to an individual re-fetch if the
    # cached copy turns out expired (rare).
    _prior_expire = session.sync_session.expire_on_commit
    session.sync_session.expire_on_commit = False
    try:
        upgraded = 0
        marked_ids: list = []
        # Subset of marked_ids that ALSO earn the terminal missing-from-diff
        # marker: provenance was present, re-evaluation still failed.
        terminal_ids: list = []

        # Batch-fetch all candidate tasks in one query (eliminates N+1).
        _batch_result = await session.execute(
            select(Task).where(Task.id.in_(task_ids))
        )
        tasks_by_id: dict = {t.id: t for t in _batch_result.scalars().all()}

        for task_id in task_ids:
            task = tasks_by_id.get(task_id)
            if task is None:
                marked_ids.append(task_id)
                continue

            # If a prior rollback inside record_evidence expired this cached
            # object, re-fetch individually (rare — IntegrityError on unique
            # constraint).  Most iterations use the batch-fetched copy.
            try:
                verdict_data = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
            except Exception:
                task = await session.get(Task, task_id)
                if task is None:
                    marked_ids.append(task_id)
                    continue
                verdict_data = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)

            if not isinstance(verdict_data, dict):
                marked_ids.append(task_id)
                continue
            if verdict_data.get("kind") != "named_test":
                marked_ids.append(task_id)
                continue
            if verdict_data.get("proven") is True:
                marked_ids.append(task_id)
                continue
            satisfied = verdict_data.get("satisfied")
            missing_from_diff = False
            if satisfied is True:
                pass
            elif satisfied is False:
                # Two re-evaluable unsatisfied classes:
                #   (a) Pre-fix verified-merge mis-scoring: the old
                #       _merge_was_test_verified treated an absent/malformed
                #       review_loop as UNVERIFIED, so a landed task whose context
                #       simply lacked a review_loop dict was recorded unsatisfied
                #       with the "did not run green" reason even though the merge
                #       was genuinely verified.
                #   (b) Missing-from-diff: the declared named_test was not in the
                #       recorded changed-file list — often because rollup children
                #       never owned a PR and the parent's changed files arrived
                #       LATE. Late-arriving provenance can flip this class, so it
                #       is admitted to re-evaluation; when still unprovable it is
                #       marked (terminally so when provenance was already in hand)
                #       and re-admitted only when fresh provenance clears the
                #       markers (see the ``missing_from_diff`` handling below).
                reason = verdict_data.get("reason")
                reason_str = reason if isinstance(reason, str) else ""
                missing_from_diff = (
                    "is not among the PR's changed files" in reason_str
                )
                if not isinstance(reason, str) or (
                    "did not run green under a real merge-test verdict" not in reason
                    and not missing_from_diff
                ):
                    marked_ids.append(task_id)
                    continue
            else:
                marked_ids.append(task_id)
                continue

            re_eval = evaluate_code_acceptance(task)
            if re_eval.proven:
                ctx = dict(task.context or {})
                ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
                    "satisfied": True,
                    "proven": True,
                    "kind": "named_test",
                    "reason": re_eval.reason,
                    "proof_ref": re_eval.proof_ref,
                    "backfilled": True,
                }
                task.context = ctx
                session.add(task)
                # Commit the verdict upgrade FIRST so a rollback inside
                # record_evidence (IntegrityError guard) does not undo it.
                await session.commit()

                from app.acceptance_criteria import record_task_merge_evidence
                try:
                    await record_task_merge_evidence(session, task, task.project_id)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "backfill_acceptance_verdicts: evidence recording failed for "
                        "task %s",
                        getattr(task, "id", None),
                        exc_info=True,
                    )
                    _batch_dirty = True  # rollback expired ORM objects
                upgraded += 1
            elif missing_from_diff:
                # Late-arriving provenance (parent-derived rollup diff or a
                # GitHub-verified list stamped after landing) may have flipped the
                # recorded verdict. A satisfied-but-unproven re-evaluation is a
                # SOFT flip out of declared_unsatisfied (parent-derived credit is
                # deliberately capped below machine proof) — re-record it so the
                # acceptance window counts the task in the satisfied_unproven
                # bucket. When re-evaluation STILL cannot locate the test, mark
                # the task so the LIMIT-200 window advances past it: terminally
                # when changed-file provenance was already present (the declared
                # test genuinely never shipped — nothing left to wait for),
                # ordinarily when provenance has not arrived yet (the candidate
                # query re-admits the class as soon as it does). Fresh provenance
                # clears both markers (record_code_acceptance_verdict and the
                # changed-file stamping sites), so re-admission never depends on
                # staying unmarked — which previously let unprovable rows occupy
                # the window forever.
                if re_eval.satisfied:
                    ctx = dict(task.context or {})
                    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
                        "satisfied": True,
                        "proven": False,
                        "kind": "named_test",
                        "reason": re_eval.reason,
                        "proof_ref": re_eval.proof_ref,
                        "backfilled": True,
                    }
                    task.context = ctx
                    session.add(task)
                    await session.commit()
                    upgraded += 1
                else:
                    marked_ids.append(task_id)
                    if "_pr_changed_files" in (task.context or {}):
                        terminal_ids.append(task_id)
            else:
                # Re-evaluation could not prove — stamp marker so the
                # LIMIT-200 window advances past this task in future calls.
                marked_ids.append(task_id)

        # Stamp backfill-checked marker on every examined task that was NOT
        # upgraded, so the query window advances past tasks that can never be
        # proven (wrong verdict kind, not satisfied, or re-evaluation still
        # returns proven=False). Without this marker, those tasks permanently
        # occupy the LIMIT-200 window and starve newer but later-ordered tasks
        # from ever being examined.
        if marked_ids:
            now_ts = datetime.now(timezone.utc).isoformat()
            _terminal_set = set(terminal_ids)
            # Batch-fetch all marked tasks in one query (eliminates N+1).
            _marked_result = await session.execute(
                select(Task).where(Task.id.in_(marked_ids))
            )
            marked_by_id: dict = {t.id: t for t in _marked_result.scalars().all()}
            for mid in marked_ids:
                t = marked_by_id.get(mid)
                if t is None:
                    continue
                ctx = dict(t.context or {})
                ctx["_backfill_checked_at"] = now_ts
                if mid in _terminal_set:
                    ctx[BACKFILL_MISSING_DIFF_TERMINAL_KEY] = now_ts
                t.context = ctx
                session.add(t)
            await session.commit()

        if upgraded:
            logger.info(
                "backfill_acceptance_verdicts: upgraded %d task(s)",
                upgraded,
            )
        return upgraded
    finally:
        session.sync_session.expire_on_commit = _prior_expire


# ---------------------------------------------------------------------------
# Release-qualifier acceptance_window sub_scores invariant guard
# ---------------------------------------------------------------------------

#: The five keys emitted by ``_compute_acceptance_window_aggregate`` and merged
#: into ``last_qualification["sub_scores"]``.  The helper is fail-safe: it
#: returns ``[]`` when NONE of these keys are present (legacy verdicts).
_ACCEPTANCE_WINDOW_KEYS: frozenset[str] = frozenset(
    {
        "acceptance_window_total",
        "acceptance_window_verified",
        "acceptance_window_fraction",
        "acceptance_window_write_total",
        "acceptance_window_total_all_merges",
    }
)

# The two keys introduced by the write-task-population fix.  Their absence means
# the verdict was written by pre-fix code (3-key schema) and should be skipped by
# invariant checks that only apply to the new schema.
_ACCEPTANCE_WINDOW_POPULATION_KEYS: frozenset[str] = frozenset(
    {"acceptance_window_write_total", "acceptance_window_total_all_merges"}
)


def acceptance_window_sub_scores_violations(sub_scores: dict) -> list[str]:
    """Return invariants VIOLATED by the ``acceptance_window_*`` fields in ``sub_scores``.

    Empty list ⇒ every guarantee holds (or the fields are absent — legacy-safe).
    Pure and read-only: never mutates, never raises. Fail-safe over legacy
    verdicts that predate the acceptance-window keys.

    Invariants checked (matching the population contract in
    ``_compute_acceptance_window_aggregate``):
      * ``verified >= 0``
      * ``write_total >= 0``
      * ``verified <= write_total``
      * ``write_total <= total_all_merges``
      * ``fraction is None iff write_total == 0, else fraction == verified / write_total``
    """
    # Skip when the new population keys are absent — this covers both the zero-key
    # case (no acceptance_window fields at all) and the pre-fix 3-key schema
    # (acceptance_window_total/verified/fraction without write_total/total_all_merges).
    # Checking for the new keys rather than any of the 5 prevents false violations
    # on healthy legacy verdicts where write_total would default to 0.
    if not any(k in sub_scores for k in _ACCEPTANCE_WINDOW_POPULATION_KEYS):
        return []

    violations: list[str] = []

    verified: int = int(sub_scores.get("acceptance_window_verified") or 0)
    write_total: int = int(sub_scores.get("acceptance_window_write_total") or 0)
    total_all: int = int(sub_scores.get("acceptance_window_total_all_merges") or 0)
    fraction = sub_scores.get("acceptance_window_fraction")

    if verified < 0:
        violations.append(f"acceptance_window_verified ({verified}) is negative")
    if write_total < 0:
        violations.append(f"acceptance_window_write_total ({write_total}) is negative")
    if verified > write_total:
        violations.append(
            f"acceptance_window_verified ({verified}) exceeds "
            f"acceptance_window_write_total ({write_total})"
        )
    if write_total > total_all:
        violations.append(
            f"acceptance_window_write_total ({write_total}) exceeds "
            f"acceptance_window_total_all_merges ({total_all})"
        )
    if write_total == 0:
        if fraction is not None:
            violations.append(
                f"acceptance_window_fraction ({fraction}) is not None "
                "with zero write_total (expected None)"
            )
    else:
        expected = verified / write_total
        if fraction is None:
            violations.append(
                f"acceptance_window_fraction is None but write_total ({write_total}) > 0"
            )
        elif fraction != expected:
            violations.append(
                f"acceptance_window_fraction ({fraction}) != "
                f"verified/write_total ({verified}/{write_total} = {expected})"
            )

    return violations


# ---------------------------------------------------------------------------
# Required-diff-paths completion contract
# ---------------------------------------------------------------------------


def check_required_diff_paths(
    task: Task, changed_files: list[str]
) -> list[str]:
    """Return declared required_diff_paths that are absent from the PR diff.

    When the task's first_payload declares ``required_diff_paths``, every listed
    path must appear in ``changed_files``. The comparison is exact-string (after
    stripping leading slashes so both ``backend/app/foo.py`` and
    ``/backend/app/foo.py`` normalise identically). Returns an empty list when
    all declared paths are present, no paths are declared, or ``changed_files``
    is empty and no paths are declared (fail-open for undeclared tasks).

    Callers that receive a non-empty return value MUST reject the PR submission
    with an actionable error naming the missing paths — fail-closed when declared.
    """
    first_payload = task.first_payload or {}
    required: list[str] | None = first_payload.get("required_diff_paths")
    if not required:
        return []

    changed_normalised = {p.lstrip("/") for p in changed_files if isinstance(p, str)}
    missing = [
        p for p in required
        if isinstance(p, str) and p.lstrip("/") not in changed_normalised
    ]
    return missing
