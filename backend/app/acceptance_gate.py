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


import hashlib
import json
import logging
import operator
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from app.ci_resolution import is_degraded_unverified_reason
from app.landed_signal import LANDED_MERGE_SHA_KEY
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

# The release-qualifier acceptance-window population contract lives in a pure
# sibling module so this chronic gate stays below its existing size cap.  Keep
# these names importable here: metrics sweeps and older callers use this module
# as their canonical compatibility surface.
from app.acceptance_window_invariants import (  # noqa: F401
    _ACCEPTANCE_WINDOW_KEYS,
    _ACCEPTANCE_WINDOW_POPULATION_KEYS,
    acceptance_window_sub_scores_violations,
)
from app.acceptance_provenance import (
    COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY,
    PARENT_DERIVED_CHANGED_FILES_KEY,
    PR_CHANGED_FILES_STAMP_SKIPPED_KEY,
    ChangedFilesStampSkipReason,
    stamp_parent_derived_changed_files_signal as _stamp_parent_derived_signal,
    stamp_commit_range_derived_changed_files_signal as _stamp_commit_range_signal,
    stamp_changed_files_skip_reason,
)
_MODULE_SIZE_GOVERNANCE_CAP: int = 3448

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

# A finding-derived fix is not proven merely because its PR landed. This marker
# is reserved for an explicit current-tree verification result. The verifier
# must name every finding it checked and the exact landed commit.
FINDING_CLEARANCE_VERIFICATION_KEY = "finding_clearance_verification"

# Durable rule-version stamped onto every recorded acceptance verdict (rungs
# 1-2 of design doc c50453ef). A later qualification reads this field to tell
# which recorded rows were judged by a now-retired rule and re-derive them via
# the pure :func:`evaluate_acceptance` core instead of trusting a stale verdict.
# Bumped whenever the acceptance evaluation rules change. Consumed by
# :func:`effective_acceptance_verdict` (rung 3), which re-derives any recorded
# row whose stamp predates the current rule.
#
# v3: rollup children now carry an authoritative changed-file signal derived from
# their own immutable PR commit range.  That provenance is distinct from the
# parent's union diff, so a matching named test may earn machine proof.
#
# v2: the evaluation rules changed this version — :func:`_merge_acceptance_test_block`
# now also reads the inherited rollup-child block (:data:`INHERITED_ACCEPTANCE_BLOCK_KEY`)
# and the window-corroboration marker semantics changed, so a row stamped under v1
# can score differently under the current core. Bumping retires those v1 stamps so
# rung 3 re-derives them instead of trusting a verdict the retired rule produced.
ACCEPTANCE_RULE_VERSION = 3

# ``rederived_by`` stamped on a re-derived verdict (design doc c50453ef §5).
# Identifies the actor that re-scored already-merged work so an auditor can tell
# a land-time stamp from a qualification-time correction. Every re-derivation
# goes through :func:`effective_acceptance_verdict`, which runs at qualification
# time inside the qualifier's acceptance-window tally — hence the qualifier label.
REDERIVED_BY_QUALIFIER = f"qualifier:v{ACCEPTANCE_RULE_VERSION}"

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


def _context_changed_files(context: Mapping[str, Any] | None) -> list[str]:
    """The PR's changed-file paths recorded on a task's context, as strings.

    Context-shaped variant of :func:`_changed_files` so the pure evaluation
    core (:func:`evaluate_acceptance`) never needs a ``Task`` object.
    """
    raw = (context or {}).get("_pr_changed_files")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str) and p.strip()]


def _changed_file_provenance_available(context: Mapping[str, Any] | None) -> bool:
    """Whether the context contains a recorded (possibly empty) PR diff list.

    An empty list is authoritative evidence of an empty diff. A missing or
    malformed value means the backend never learned the diff and must not
    classify that absence as proof that a declared test was omitted.
    """
    raw = (context or {}).get("_pr_changed_files")
    return isinstance(raw, list) and all(
        isinstance(path, str) and bool(path.strip()) for path in raw
    )


def _changed_files(task: Task) -> list[str]:
    """The PR's changed-file paths recorded on the task, as a list of strings."""
    return _context_changed_files(task.context)


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


def stamp_parent_derived_changed_files_signal(paths: list[str]) -> dict[str, Any]:
    """Stamp a rollup child inheriting its parent's combined diff."""
    return _stamp_parent_derived_signal(
        paths,
        stamp_changed_files_signal=stamp_changed_files_signal,
        parent_derived_key=PARENT_DERIVED_CHANGED_FILES_KEY,
    )


def stamp_commit_range_derived_changed_files_signal(paths: list[str]) -> dict[str, Any]:
    """Stamp a rollup child's exact PR-range changed-file provenance."""
    return _stamp_commit_range_signal(
        paths,
        stamp_changed_files_signal=stamp_changed_files_signal,
        parent_derived_key=PARENT_DERIVED_CHANGED_FILES_KEY,
    )


async def stamp_pr_changed_files_at_landing(
    task: Task,
    session: Any,
    *,
    pr_number: int | str | None = None,
    github_credentials: tuple[str, str, str] | None = None,
) -> bool:
    """Stamp GitHub's changed-file list before recording a landing verdict.

    The landed-signal writer is the single consumer of code acceptance verdicts,
    but many merge lanes reach it. Keeping this acquisition here makes every
    lane use the same authoritative PR diff while preserving the merge contract:
    a credential, PR lookup, or GitHub failure records a bounded reason and never
    prevents the real landing from being written.

    ``github_credentials`` is accepted for callers that already resolved the
    merge PR (notably CI completion); other callers resolve credentials from the
    task and session. A parent-derived rollup child is deliberately left alone:
    its parent union is a known lower-confidence signal and must not be replaced
    with a lookup for the parent's PR.
    """
    context = dict(task.context or {})
    if context.get("_pr_changed_files_github_verified") is True:
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.already_stamped
        )
        task.context = context
        return False
    if context.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True:
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.rollup_parent_pr
        )
        task.context = context
        return False

    if pr_number is None:
        from app.ci_merge_helpers import effective_pr_number

        pr_number = effective_pr_number(task)
    if pr_number is None or isinstance(pr_number, bool) or not str(pr_number).strip():
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.pr_number_unavailable
        )
        task.context = context
        return False

    credentials = github_credentials
    if credentials is None:
        try:
            from app.routers.agent import get_github_token_for_task

            credentials = await get_github_token_for_task(task.id, session)
        except Exception:  # provenance is best-effort at landing
            stamp_changed_files_skip_reason(
                context, ChangedFilesStampSkipReason.github_credentials_unavailable
            )
            task.context = context
            logger.warning(
                "changed-file provenance credentials unavailable for landed task %s",
                getattr(task, "id", None),
                exc_info=True,
            )
            return False

    if (
        not isinstance(credentials, tuple)
        or len(credentials) != 3
        or not all(isinstance(value, str) and value.strip() for value in credentials)
    ):
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.github_credentials_unavailable
        )
        task.context = context
        return False

    try:
        from app.github import get_pr_files

        raw_files = await get_pr_files(
            credentials[0],
            credentials[1],
            credentials[2],
            int(pr_number),
            str(task.project_id) if task.project_id is not None else None,
        )
        paths = [
            item["filename"]
            for item in (raw_files or [])
            if isinstance(item, dict)
            and isinstance(item.get("filename"), str)
            and item["filename"].strip()
        ]
    except Exception:  # provenance must never abort a landing
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.diff_fetch_failed
        )
        task.context = context
        logger.warning(
            "changed-file provenance fetch failed for landed task %s (PR #%s)",
            getattr(task, "id", None),
            pr_number,
            exc_info=True,
        )
        return False

    if not paths:
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.no_diff_returned
        )
        task.context = context
        return False

    context.pop(PR_CHANGED_FILES_STAMP_SKIPPED_KEY, None)
    context.update(stamp_changed_files_signal(paths, github_verified=True))
    task.context = context
    return True


def _is_test_path(path: str) -> bool:
    """Whether ``path`` names a test file (under a tests/testing dir or by name)."""
    return _looks_like_test_file(path)


def _paths_match(changed: str, declared: str) -> bool:
    """Whether a changed-file path refers to the declared test target.

    Tolerant of leading-directory differences between the agent-reported changed
    paths and the declared ``test_path`` (one may be repo-relative, the other a
    subpath): an exact match, a suffix match in either direction, or a basename
    match all count. Runtime values that are not strings and blank paths are not
    valid matches. Normalize Windows separators before comparing because GitHub,
    agents, and declarations may use different path conventions.
    """
    if not isinstance(changed, str) or not isinstance(declared, str):
        return False
    changed = changed.replace("\\", "/")
    declared = declared.replace("\\", "/")
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
    # Validates CI configuration directly (pyproject.toml addopts, the
    # committed .test_durations file, module_size_limits.json) rather than a
    # single production module named ``shard_balance.py`` — no such module
    # exists, so its stem cannot ground via the per-module heuristic.
    "backend/testing/test_shard_balance.py",
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


def _merge_was_test_verified(context: Mapping[str, Any] | None) -> bool:
    """Whether the merge was gated by a REAL test verdict.

    Mirrors the canonical ``landed_signal.is_verified_merge`` predicate (and
    this module's own ``_context_merge_verification_status``): the no-CI merge
    gate stamps ``review_loop["unverified_merge_reason"]`` when it legitimately
    "falls open" and merges WITHOUT running real tests. Presence of that reason
    means the merge was NOT test-verified — returns False. An ABSENT or
    malformed review_loop carries no fall-open reason, so the merge is treated
    as VERIFIED — exactly the fail-safe default every other consumer uses. Only
    an explicit, non-empty ``unverified_merge_reason`` returns False.

    Context-shaped (a bare ``context`` dict, not a ``Task``) so the pure
    evaluation core (:func:`evaluate_acceptance`) can re-derive verdicts from
    persisted merge records.
    """
    review_loop = (context or {}).get(REVIEW_LOOP_KEY)
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

# Durable distilled descriptor the no-CI merge monitor stamps alongside
# ``acceptance_test_targeted_green`` (the block's ``selector`` or ``descriptor``)
# so the gate can corroborate that the targeted-green signal actually names the
# DECLARED test rather than some other test the agent happened to run green.
ACCEPTANCE_TEST_TARGETED_DESCRIPTOR_KEY = "acceptance_test_targeted_descriptor"


def acceptance_test_block_matches_declared(block: Any, declared_path: str) -> bool:
    """Whether a merge-test acceptance block describes the DECLARED test path.

    The gate credits ``test_exists`` / ``ran_green`` from an agent-reported
    merge-test block; that credit is only honest when the block names the test
    the task actually declared. ``selector`` (the precise ``test_path`` the agent
    ran, possibly with a ``::node`` suffix) and ``descriptor`` (path-shaped, or a
    bare node id when the declaration pinned one) are each partitioned at ``::``
    and matched against ``declared_path`` via the tolerant :func:`_paths_match` —
    so ``backend/testing/test_foo.py::test_x`` names ``backend/testing/test_foo.py``.
    Returns False for an absent / malformed block or when neither field names the
    declared path, so a green run of an UNRELATED test can never be credited as
    proof of this declaration.
    """
    if not isinstance(block, dict):
        return False
    declared_path = (declared_path or "").strip()
    if not declared_path:
        return False
    for key in ("selector", "descriptor"):
        value = str(block.get(key) or "").strip()
        if not value:
            continue
        path_part, _, _ = value.partition("::")
        path_part = path_part.strip()
        if path_part and _paths_match(path_part, declared_path):
            return True
    return False


def _declared_named_test_path(payload: dict | None) -> str | None:
    """The declared named_test's FILE path from the acceptance evidence payload.

    ``None`` when the payload declared no named_test evidence (or it is
    unreadable) — the merge-test readers fail closed on a task whose declaration
    cannot be corroborated. Payload-shaped (reads the same ``first_payload`` the
    task-based readers used) so the pure evaluation core
    (:func:`evaluate_acceptance`) can re-derive verdicts from persisted merge
    records without a ``Task`` object.
    """
    evidence = read_acceptance_evidence(payload)
    if evidence is None or evidence.kind != "named_test":
        return None
    return (evidence.test_path or "").partition("::")[0].strip() or None


def _acceptance_test_targeted_green(
    context: Mapping[str, Any] | None, declared_path: str | None = None
) -> bool:
    """Whether the merge gate independently ran the declared acceptance test as a
    TARGETED run and it exited GREEN (fail-safe / defensive).

    Reads ``review_loop['acceptance_test_targeted_green']`` — the durable signal
    the no-CI monitor stamps when :func:`ci_resolution.acceptance_test_is_green`
    held — and CORROBORATES it: the signal is only honest proof when the block
    (or the descriptor the monitor distilled from it) names the DECLARED test.
    A green run of a DIFFERENT test must never prove a name-dropped declaration,
    so a signal whose identity evidence is absent or mismatched yields False.
    Returns True ONLY on a literal ``True`` value that names the declared test;
    a missing / malformed / non-dict review_loop, an older agent that never
    emitted the signal, or an identity mismatch all yield False so today's
    behavior is preserved.

    Context-shaped (a bare ``context`` dict plus the declared test path, not a
    ``Task``) so the pure evaluation core (:func:`evaluate_acceptance`) can
    re-derive verdicts from persisted merge records.
    """
    if not declared_path:
        return False
    review_loop = (context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return False
    if review_loop.get(ACCEPTANCE_TEST_TARGETED_GREEN_KEY) is not True:
        return False
    block = review_loop.get(MERGE_ACCEPTANCE_BLOCK_KEY)
    if isinstance(block, dict):
        return acceptance_test_block_matches_declared(block, declared_path)
    descriptor = review_loop.get(ACCEPTANCE_TEST_TARGETED_DESCRIPTOR_KEY)
    if isinstance(descriptor, str) and descriptor.strip():
        return acceptance_test_block_matches_declared(
            {"selector": descriptor}, declared_path
        )
    # A targeted-green signal with no identity evidence is not corroborable.
    return False


# Durable NORMALIZED acceptance-test block the no-CI merge monitor persists onto
# ``review_loop`` (see ``orchestrator_ci_monitor``). Distinct from
# ``review_loop['verify_tests_verdict']``, which holds the classifier's outcome
# STRING ("pass"/"fail"/"undetected") and therefore can never carry the block.
MERGE_ACCEPTANCE_BLOCK_KEY = "verify_tests_acceptance_block"

# Durable top-level context key where a ROLLUP CHILD receives its parent's
# normalized acceptance-test block when the parent's block names the CHILD's
# declared test path (rung 6 of design doc c50453ef, closing U1). Rollup
# children merge via the parent's single PR and never own a ``review_loop``, so
# without this a child's declared test that the parent's CI genuinely ran green
# reads as "did not run green" and fails closed forever. Lives at the TOP level
# (like ``_pr_changed_files``) because the child's ``review_loop`` is
# deliberately absent — an inherited block must never be mistaken for the
# child's own merge evidence. The propagation site (``rollup_management``)
# copies the parent's block ONLY when its ``selector``/``descriptor`` matches
# the child's declared test path; an unmatched selector propagates nothing.
INHERITED_ACCEPTANCE_BLOCK_KEY = "_inherited_acceptance_block"


def _merge_acceptance_test_block(context: Mapping[str, Any] | None) -> dict | None:
    """Read the declared acceptance test's normalized merge-test block, defensively.

    Returns the block the no-CI monitor persisted under
    ``review_loop['verify_tests_acceptance_block']`` (the output of
    :func:`ci_resolution.acceptance_test_block`, carrying
    ``present``/``ran``/``status``/``passed``), else ``None``. A missing /
    malformed / non-dict review_loop, or a legacy task merged before the monitor
    persisted the block, yields ``None`` — so every caller MUST still fall back to
    :func:`_acceptance_test_targeted_green`, which those tasks do carry.

    For a ROLLUP CHILD (which never owns a ``review_loop``), the reader falls
    back to the top-level :data:`INHERITED_ACCEPTANCE_BLOCK_KEY` block the rollup
    propagation stamped from its parent — the block describes the child's
    declared test (the propagation site matches the selector), so ``test_exists``
    / ``ran_green`` for an unmodified declared test on a rollup child are
    readable from the same normalized shape. ``_acceptance_test_targeted_green``
    deliberately does NOT read the inherited block: the targeted-green signal is
    per-PR evidence and is never propagated to a child.

    Context-shaped (a bare ``context`` dict, not a ``Task``) so the pure
    evaluation core (:func:`evaluate_acceptance`) can re-derive verdicts from
    persisted merge records.
    """
    review_loop = (context or {}).get(REVIEW_LOOP_KEY)
    if isinstance(review_loop, dict):
        block = review_loop.get(MERGE_ACCEPTANCE_BLOCK_KEY)
        if isinstance(block, dict):
            return block
    inherited = (context or {}).get(INHERITED_ACCEPTANCE_BLOCK_KEY)
    return inherited if isinstance(inherited, dict) else None


# Internal, BACKEND-computed marker recording whether the declared acceptance
# test's path (and node id, when declared) was independently corroborated against
# the repo's tree at the merge commit. Underscore-prefixed like
# ``_pr_changed_files``: computed by the backend (see
# :func:`corroborate_acceptance_test_at_merge`), NEVER by the agent, so the
# existence predicate can fail closed without trusting the agent's ``present``
# word. Value is ``True`` (path + node confirmed present), ``False`` (path or
# node confirmed ABSENT), or absent (not yet computed → callers fall back to the
# agent-reported evidence rather than suppress a genuinely-existing test on
# missing data).
ACCEPTANCE_TEST_CORROBORATED_KEY = "_acceptance_test_corroborated"

# Durable "corroboration was already attempted at this merge commit" marker,
# written by the release qualifier's acceptance-window backfill pass. A
# corroboration that returns False/None stamps NO
# :data:`ACCEPTANCE_TEST_CORROBORATED_KEY`, so without this marker the same
# tasks would be re-read from GitHub on every subsequent qualifier drive
# forever. The value is the merge sha that was read (not a bare boolean), so a
# task that later lands on a NEW merge commit is eligible for a fresh attempt.
WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY = "_acceptance_corroboration_attempted_sha"


def _declared_test_reference(
    evidence: AcceptanceEvidence,
) -> tuple[str, str | None]:
    """Split a ``named_test`` declaration into ``(test_path, node_id)``.

    The node id may be carried inline (``path::node_id`` in ``test_path``) or in
    the separate ``test_id`` field; the inline form wins. Returns an empty path
    for a declaration that names no test file.
    """
    raw = (evidence.test_path or "").strip()
    path_part, sep, inline_node = raw.partition("::")
    path_part = path_part.strip()
    node_id = (inline_node.strip() if sep else "") or (
        evidence.test_id or ""
    ).strip()
    return path_part, (node_id or None)


def _declared_test_id_present(node_id: str, file_text: str) -> bool:
    """Whether *node_id* names a real test node inside *file_text*.

    Matches the id's leaf node (the segment after the last ``::``, so a full
    pytest node like ``TestSeam::test_x`` reduces to ``test_x``) as a standalone
    identifier. A fabricated id that appears nowhere in the file fails closed; a
    present id sharing only a word-boundary prefix with something else
    (``test_x`` vs ``test_xy``) does not false-positive.
    """
    leaf = node_id.rsplit("::", 1)[-1].strip()
    if not leaf:
        return False
    return re.search(rf"\b{re.escape(leaf)}\b", file_text) is not None


# Top-level monorepo roots a declared test path may have dropped. A declaration
# already carrying one of these prefixes is used as-is; otherwise every root is
# tried, so a prefix-dropped admin-ui/agent declaration can never corroborate as
# a definite absence merely because only the backend/ variant was probed.
_MONOREPO_TEST_ROOTS = ("backend/", "admin-ui/", "agent/")


def _candidate_test_paths(declared_path: str) -> list[str]:
    """Repo-relative candidate paths for a declared test path.

    Returns the declared path plus, when it does not already carry one of the
    :data:`_MONOREPO_TEST_ROOTS` prefixes, a variant under EACH root — so a
    declaration that drops its monorepo prefix (backend, admin-ui, or agent)
    still corroborates against the real tree. Deduplicated, order preserved.
    """
    declared = declared_path.strip().lstrip("./")
    candidates = [declared]
    if not declared.startswith(_MONOREPO_TEST_ROOTS):
        candidates.extend(root + declared for root in _MONOREPO_TEST_ROOTS)
    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


async def corroborate_acceptance_test_at_merge(
    task: Task,
    session: Any,
    project: Any = None,
    *,
    branch: str | None = None,
    commit_sha: str | None = None,
) -> bool | None:
    """Backend-side corroboration that the declared named_test exists in the repo
    tree at the merge commit — NOT taken on the agent's word.

    When *commit_sha* is provided (the merged PR's merge commit), the tree is
    read at that EXACT immutable commit — never through the branch-tip cache,
    whose short TTL can still serve the PRE-merge tip moments after a merge and
    would report a just-added test absent. Callers at a merged-landing site
    MUST pass it whenever they know the merge commit. Without it, the read
    falls back to *branch* (defaulting to the project's integration branch).

    Tri-state:

      * ``True`` — the declared test path (and, when declared, its node id) is
        confirmed present in the repo tree on *branch* (defaulting to the
        project's integration branch).
      * ``False`` — the declared path is confirmed ABSENT, or the declared node
        id is absent from a present file. This is a backend-refuted
        non-existence verdict and is never fail-open.
      * ``None`` — indeterminate: the task declares no ``named_test``, the
        project has no GitHub credentials, the tree snapshot is unavailable, or
        a present file's text could not be read. Indeterminate is NOT absence —
        callers fall back to the agent-reported evidence so a genuinely-existing
        test is never suppressed on missing data.

    The caller stamps the result onto the task's context under
    :data:`ACCEPTANCE_TEST_CORROBORATED_KEY` so the synchronous
    :func:`_acceptance_test_exists_at_merge` predicate can fail closed without a
    network call. Never raises.
    """
    evidence = read_acceptance_evidence(task.first_payload)
    if evidence is None or evidence.kind != "named_test":
        return None
    declared_path, declared_id = _declared_test_reference(evidence)
    if not declared_path:
        return None

    from app.source_ref_validation import resolve_branch_verification_context

    try:
        ctx = await resolve_branch_verification_context(session, project)
    except Exception:  # noqa: BLE001 — corroboration must degrade, never raise
        return None
    if branch:
        ctx["branch"] = branch
    token = ctx.get("token")
    owner = ctx.get("owner")
    repo = ctx.get("repo")
    if not (token and owner and repo):
        return None
    if not commit_sha and not ctx.get("branch"):
        return None

    from app.github_repo_snapshot import file_text_at_commit, file_text_on_branch

    saw_indeterminate = False
    for path in _candidate_test_paths(declared_path):
        try:
            if commit_sha:
                exists, text = await file_text_at_commit(
                    path,
                    token=token,
                    owner=owner,
                    repo=repo,
                    commit_sha=commit_sha,
                    project_id=ctx.get("project_id"),
                )
            else:
                exists, text = await file_text_on_branch(
                    path,
                    token=token,
                    owner=owner,
                    repo=repo,
                    branch=ctx["branch"],
                    project_id=ctx.get("project_id"),
                )
        except Exception:  # noqa: BLE001 — corroboration must degrade, never raise
            saw_indeterminate = True
            continue
        if exists is True:
            if declared_id:
                if text is None:
                    return None
                return _declared_test_id_present(declared_id, text)
            return True
        if exists is None:
            saw_indeterminate = True
    if saw_indeterminate:
        return None
    return False


async def corroborate_acceptance_for_landing(
    task: Task,
    session: Any,
    project: Any = None,
    *,
    branch: str | None = None,
    commit_sha: str | None = None,
) -> bool | None:
    """Run :func:`corroborate_acceptance_test_at_merge` and stamp its marker.

    This is the reusable "corroborate + stamp" wrapper every integration-landing
    path calls BEFORE it records the code-acceptance verdict, so a declared test
    that is ABSENT from the PR's diff but PRESENT in the merged tree can mint
    machine-proven coverage (see :func:`_evaluate_named_test` — the proven mint
    for a not-in-diff test requires the backend's own
    :data:`ACCEPTANCE_TEST_CORROBORATED_KEY` to be ``True``). Previously only the
    orchestrator CI monitor stamped the marker; every other landing path
    (:func:`app.landed_signal.mark_landed_on_integration` callers) left it
    absent and the gate fell back to agent self-report, capping a genuinely
    existing not-in-diff test at soft/unproven credit forever.

    Stamps the marker onto ``task.context`` ONLY when corroboration produced a
    definitive tri-state (``True``/``False``); an indeterminate ``None`` leaves
    the marker absent so the evaluation core falls back to agent-reported
    evidence. Best-effort — never raises, so a corroboration failure can never
    abort a real merge landing. The caller is responsible for committing.

    ONE EXTRA GUARD on the durable ``False``: a corroborated ABSENCE is only
    stamped when the read was PINNED to an immutable *commit_sha*. Without it
    the read goes through the branch-tip cache
    (:data:`app.github_repo_snapshot._BRANCH_SHA_TTL_S`, 60s), which moments
    after a merge can still serve the PRE-merge tip and report a test the PR
    itself just added as absent — and that ``False`` is durable and overrides
    the agent's targeted-green evidence in
    :func:`_acceptance_test_exists_at_merge` forever. An unpinned absence
    therefore leaves the marker unstamped (today's fallback behaviour) rather
    than recording a verdict the cache cannot support. A ``True`` needs no such
    guard: a stale tip proves the test existed even BEFORE the merge.

    Returns the corroboration result (``True``/``False``/``None``) so a caller
    that needs a CONSERVATIVE backfill (only upgrade, never downgrade — see
    ``_compute_acceptance_window_aggregate``) can decide not to re-record on a
    corroborated absence.
    """
    try:
        result = await corroborate_acceptance_test_at_merge(
            task,
            session,
            project=project,
            branch=branch,
            commit_sha=commit_sha,
        )
        if result is True or (result is False and commit_sha):
            task.context = {
                **(task.context or {}),
                ACCEPTANCE_TEST_CORROBORATED_KEY: result,
            }
        return result
    except Exception:
        logger.warning(
            "corroborate_acceptance_for_landing failed for task %s",
            getattr(task, "id", None),
            exc_info=True,
        )
        return None


def _acceptance_test_exists_at_merge(
    context: Mapping[str, Any] | None, declared_path: str | None = None
) -> bool:
    """Whether the declared acceptance test is confirmed to EXIST in the repo at
    the merge commit — independent of whether the PR's diff touches it.

    True when the merge gate physically executed the declared test as a targeted
    run (``acceptance_test_targeted_green`` — a test that ran must exist), or a
    merge-test verdict block records the test as ``present``. Both paths are
    CORROBORATED against the DECLARED test path, so a green run of some OTHER
    test is never read as existence of the declaration. Defensive: absent /
    malformed evidence, or a block that names a different test, yields False, so
    a name-dropped test that can be shown to exist nowhere is never credited.

    The agent-reported evidence is NECESSARY but no longer sufficient. When the
    backend has independently corroborated the declared test path against the
    repo tree (:data:`ACCEPTANCE_TEST_CORROBORATED_KEY`, computed by
    :func:`corroborate_acceptance_test_at_merge`), a corroborated ABSENCE
    overrides the agent's ``present`` word fail-closed — an agent asserting a
    test exists that the backend can prove does not is never credited. An
    indeterminate / not-yet-computed corroboration leaves today's behavior
    intact, so a genuinely-existing test is never suppressed on missing data.

    Context-shaped (a bare ``context`` dict plus the declared test path, not a
    ``Task``) so the pure evaluation core (:func:`evaluate_acceptance`) can
    re-derive verdicts from persisted merge records.
    """
    # A test can only be confirmed to exist if something actually claimed to run
    # it (targeted green) or a merge-test verdict NAMING THE DECLARED TEST
    # records it present.
    if not _acceptance_test_targeted_green(context, declared_path):
        block = _merge_acceptance_test_block(context)
        if not declared_path or not acceptance_test_block_matches_declared(
            block, declared_path
        ):
            return False
        if not (block and block.get("present") is True):
            return False
    # Backend-side corroboration against the merged tree: a corroborated absence
    # is a non-existence verdict even when the agent reported the test present.
    return (context or {}).get(ACCEPTANCE_TEST_CORROBORATED_KEY) is not False


def _acceptance_test_ran_green_at_merge(
    context: Mapping[str, Any] | None, declared_path: str | None = None
) -> bool:
    """Whether the declared acceptance test is confirmed to have RAN GREEN under a
    real merge-test verdict — independent of the PR's diff.

    True when the targeted-green signal was stamped (the merge gate independently
    executed ONLY the declared test and it exited green), or a merge-test verdict
    block records the declared test as present, ran, with status ``"ran"`` and
    ``passed``. Both paths are CORROBORATED against the DECLARED test path, so a
    green run of some OTHER test is never credited. A test that exists but did
    not run green (skipped / absent / failed) yields False. Same
    backend-corroboration override as :func:`_acceptance_test_exists_at_merge`: a
    corroborated absence means the declared test was never really there, so no
    green run can be credited for it.

    Context-shaped (a bare ``context`` dict plus the declared test path, not a
    ``Task``) so the pure evaluation core (:func:`evaluate_acceptance`) can
    re-derive verdicts from persisted merge records.
    """
    if _acceptance_test_targeted_green(context, declared_path):
        ran_green = True
    else:
        block = _merge_acceptance_test_block(context)
        if not declared_path or not acceptance_test_block_matches_declared(
            block, declared_path
        ):
            return False
        ran_green = bool(
            block
            and block.get("present") is True
            and block.get("ran") is True
            and block.get("status") == "ran"
            and block.get("passed") is True
        )
    if not ran_green:
        return False
    return (context or {}).get(ACCEPTANCE_TEST_CORROBORATED_KEY) is not False


def _evaluate_named_test(
    evidence: AcceptanceEvidence,
    changed: list[str],
    verified: bool,
    changed_file_provenance_available: bool = True,
    truncated: bool = False,
    parent_derived: bool = False,
    targeted_green: bool = False,
    test_exists: bool = False,
    ran_green: bool = False,
    block_describes_other_test: bool = False,
    corroborated: bool | None = None,
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
    credit above; nothing overrides it — EXCEPT a backend-refuted
    :data:`ACCEPTANCE_TEST_CORROBORATED_KEY` (``corroborated is False``). That is
    not a data artifact: the backend checked the EXACT merge-commit tree and
    confirmed the declared test exists nowhere, so the truncation premise ("may
    ship beyond the stored prefix") is provably defeated and the hard fail below
    applies. Where the list is complete (``truncated=False``), the plain
    hard-fail below already applies and is genuinely authoritative because every
    changed path is stored.

    ``parent_derived`` marks that the changed-file list was INHERITED from a
    rollup parent's combined diff (the union of all siblings' changes) rather than
    the child's own PR. Because a child's declared test could physically live in a
    sibling's slice of that union, a parent-derived match is real enough to clear
    ``declared_unsatisfied`` (satisfied) but must NOT mint machine ``proven``
    credit — the final proof branch is capped at ``satisfied=True, proven=False``.

    ``block_describes_other_test`` marks that a merge-test acceptance block was
    recorded but it names a DIFFERENT test than the declared ``test_path`` — a
    green run of some other test is never proof of this declaration. When the
    credit would rest on that block (the declared test is absent from the diff,
    or the merge fell open and the targeted-green signal is the only green
    evidence), the gate fails closed with a distinct, actionable reason. An
    in-diff credit under a CI-VERIFIED merge does not rest on the block, so a
    stray mismatch never penalizes an independently-proven task. Neither does the
    TRUNCATION credit: there the credit rests on the absence being inconclusive
    (the declared test may ship beyond the stored 50-path prefix), not on the
    block, so ``truncated`` with the test absent keeps its soft
    ``satisfied=True, proven=False`` outcome regardless of a mismatched block —
    consistent with the truncation invariant above that nothing overrides it.

    ``corroborated`` is the backend's OWN tri-state tree check of the declared
    test's existence at the merge commit (the stamped
    :data:`ACCEPTANCE_TEST_CORROBORATED_KEY` value). For a test ABSENT from the
    PR's diff, existence-at-merge is the anti-fraud guard, and the agent-reported
    inputs behind ``test_exists``/``ran_green``/``targeted_green`` are exactly
    what a fraudulent declaration would fabricate — so the proven mint for that
    shape additionally requires ``corroborated is True``. Anything else
    (``None``: never computed / credentials missing / snapshot unavailable) caps
    the credit at ``satisfied=True, proven=False``: honest missing data never
    penalizes the merge, but the agent's word alone never mints machine proof.
    A corroborated ABSENCE (``False``) is stronger still: it overrides even the
    truncation soft credit — a >50-file PR whose declared test the backend proves
    exists nowhere cannot claim the "may ship beyond the stored prefix" excuse.
    An in-diff test is unaffected — presence in the GitHub-sourced diff is
    already backend-grounded evidence.
    """
    declared_path = (evidence.test_path or "").partition("::")[0].strip()
    test_present = any(_paths_match(cf, declared_path) for cf in changed)
    # Corroboration gate: a merge-test block that names a DIFFERENT test is never
    # proof of this declaration. Only the block-dependent credits are blocked —
    # an in-diff test under a CI-verified merge is proven independently of the
    # block, so a stray mismatch there does not fail a genuinely proven task. The
    # truncation credit below is likewise block-independent: it rests on the
    # absence being INCONCLUSIVE, not on the block, so a stray mismatch must not
    # convert a soft truncation credit into a hard fail (see the truncation
    # invariant documented above: nothing overrides it).
    if (
        block_describes_other_test
        and not (test_present and verified)
        and not (truncated and not test_present)
    ):
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            f"declared named_test {declared_path!r} cannot be credited from the "
            "merge-test verdict: the recorded acceptance-test block describes a "
            "DIFFERENT test than the declared test_path — a green run of another "
            "test does not prove this declaration",
        )
    if not test_present:
        if not changed_file_provenance_available:
            return AcceptanceGateVerdict(
                True,
                False,
                evidence.kind,
                f"declared named_test {declared_path!r} could not be evaluated "
                "because PR changed-file provenance was unavailable — "
                "_pr_changed_files was never recorded",
            )
        if truncated and corroborated is False:
            # The truncation soft credit rests on the premise that the declared
            # test "may ship beyond the stored 50-path prefix". A backend-REFUTED
            # corroboration defeats that premise: the backend checked the exact
            # merge-commit tree and confirmed the declared test exists NOWHERE, so
            # its absence is proven rather than inconclusive. This closes the
            # large-rollup name-drop shape — a >50-file PR whose declared test
            # never shipped must not earn the truncation credit.
            return AcceptanceGateVerdict(
                True,
                False,
                evidence.kind,
                f"declared named_test {declared_path!r} is not among the PR's "
                "changed files — the changed-file list was truncated (>50 files, "
                "only the first 50 paths are stored), but the backend "
                "corroborated at the merge commit that the declared test does "
                "not exist in the repo tree, so absence is proven rather than "
                "inconclusive",
            )
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
    if not test_present and corroborated is not True:
        # The declared test is NOT in the PR's diff, so its existence at the
        # merge commit is the anti-fraud guard — and every input that guard rests
        # on here (the merge-test block, the targeted-green stamp) is
        # agent-reported. Machine ``proven`` for this shape requires the
        # backend's OWN tree check to have positively confirmed existence
        # (:data:`ACCEPTANCE_TEST_CORROBORATED_KEY` is True). Indeterminate /
        # never-computed corroboration credits soft instead: the merge stays
        # satisfied (no window monopolization, no penalty for missing data) but
        # purely agent-reported evidence can never mint ``proven``. A
        # corroborated ABSENCE never reaches here — it already forced
        # ``test_exists`` False and hard-failed above.
        return AcceptanceGateVerdict(
            True,
            True,
            evidence.kind,
            f"declared named_test {declared_path!r} is not among the PR's "
            "changed files and its existence at the merge commit rests on "
            "agent-reported evidence alone (backend corroboration "
            "indeterminate) — credited soft without machine proof",
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
    (non-test seam, semantic grounding) while skipping the merge-time
    green-run requirement. A declared test ABSENT from the PR's diff is NOT a
    structural defect: the merge-time gate credits a test that exists at the
    merge commit and ran green (backend-corroborated), and pre-merge we cannot
    know whether that holds — so absence from the diff alone is
    satisfied/unproven, with the anti-fraud existence check deferred to merge.
    The test-only-diff and ungrounded-seam hard-fails survive because the merge
    gate enforces them identically. Same fail-closed reason strings, so
    :func:`classify_unsatisfied_reason` maps them identically. Truncated /
    parent-derived input stays inconclusive (satisfied/unproven), as at merge.
    """
    declared_path = (evidence.test_path or "").partition("::")[0].strip()
    test_present = any(_paths_match(cf, declared_path) for cf in changed)
    if not test_present and truncated:
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
    if not test_present:
        # Absent from the diff but structurally sound: the merge gate credits an
        # unmodified test that exists at the merge commit and ran green (see
        # _evaluate_named_test), so absence alone must not hard-fail the open PR.
        # Existence-at-merge and the green run are merge-time checks; pre-merge
        # we can only credit soft and let the merge gate mint proof or fail.
        return AcceptanceGateVerdict(
            True,
            True,
            evidence.kind,
            f"declared named_test {declared_path!r} is not among the PR's changed "
            "files, but the merge gate credits an unmodified test that exists at "
            "the merge commit and ran green — absence from the diff is not a "
            "structural defect, so this is credited soft without machine proof "
            "before merge",
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


def _evaluate_visual(
    project_id: object,
    context: Mapping[str, Any] | None,
    evidence: AcceptanceEvidence,
) -> AcceptanceGateVerdict:
    """Fail-closed gate for a ``visual`` acceptance declaration.

    A visual declaration is satisfied only when the task context already
    carries the uploaded viewport screenshot URI for the declared
    ``(component, viewport_width)`` pair.  The shared resolver keeps the object
    name derivation consistent with the upload/download path and fails closed
    on absent or mismatched evidence.

    Context-shaped (a bare ``project_id`` + ``context``, not a ``Task``) so the
    pure evaluation core (:func:`evaluate_acceptance`) can re-derive verdicts
    from persisted merge records.
    """
    if evidence.component is None or evidence.viewport_width is None:
        return AcceptanceGateVerdict(
            True,
            False,
            evidence.kind,
            "declared visual acceptance evidence is malformed or incomplete",
        )

    resolved = resolve_visual_evidence(
        project_id,
        evidence.component,
        evidence.viewport_width,
        dict(context or {}) if context is not None else None,
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
            "(coverage percentage 0-100)",
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


def evaluate_acceptance(
    payload: dict | None,
    context: Mapping[str, Any] | None,
    project_id: object,
) -> AcceptanceGateVerdict:
    """Evaluate a task's declared acceptance evidence as a PURE function of its
    durable inputs — ``payload`` (the first_payload), ``context``, and
    ``project_id`` — with no ``Task`` ORM dependency.

    This is the re-derivable core (rung 1 of design doc c50453ef): a later
    qualification re-runs it over persisted merge records to re-score verdicts
    that were stamped under a retired rule (see :data:`ACCEPTANCE_RULE_VERSION`),
    without touching the task rows' live ORM state. Reads the declaration from
    the payload, the PR's changed files from ``context['_pr_changed_files']``,
    and whether the merge was test-verified from the no-CI gate's recorded
    reason. A task with no declaration returns a non-recorded passing verdict
    (``declared=False``), preserving today's behavior exactly.

    ``evaluate_code_acceptance(task)`` is the thin ``Task`` adapter over this
    function; both must always return the same verdict for the same
    (payload, context, project_id) triple.
    """
    evidence = read_acceptance_evidence(payload)
    if evidence is None:
        return AcceptanceGateVerdict(
            False, True, None, "no acceptance evidence declared"
        )

    changed = _context_changed_files(context)
    changed_file_provenance_available = _changed_file_provenance_available(context)
    verified = _merge_was_test_verified(context)
    truncated = bool((context or {}).get("_pr_changed_files_truncated"))
    parent_derived_files = bool(
        (context or {}).get(PARENT_DERIVED_CHANGED_FILES_KEY)
    )
    # Corroborate the merge-test evidence's identity against the DECLARED test.
    # A block (or a targeted-green signal riding without one) that names a
    # DIFFERENT test is a distinct fail-closed cause — a green run of another
    # test must never prove a name-dropped declaration. The hardened readers
    # below already refuse mismatched evidence; this flag gives the verdict a
    # specific, actionable reason instead of a misleading generic one.
    block_describes_other_test = False
    declared_path = _declared_named_test_path(payload)
    if evidence.kind == "named_test" and declared_path:
        block = _merge_acceptance_test_block(context)
        block_describes_other_test = (
            block is not None
            and not acceptance_test_block_matches_declared(block, declared_path)
        )
        if not block_describes_other_test:
            # A targeted-green signal without a persisted block (older monitor)
            # must still name the declared test — corroborate the distilled
            # descriptor the monitor stamped alongside it.
            review_loop = (context or {}).get(REVIEW_LOOP_KEY)
            if (
                isinstance(review_loop, dict)
                and review_loop.get(ACCEPTANCE_TEST_TARGETED_GREEN_KEY) is True
                and not isinstance(review_loop.get(MERGE_ACCEPTANCE_BLOCK_KEY), dict)
            ):
                descriptor = review_loop.get(ACCEPTANCE_TEST_TARGETED_DESCRIPTOR_KEY)
                block_describes_other_test = (
                    isinstance(descriptor, str)
                    and bool(descriptor.strip())
                    and not acceptance_test_block_matches_declared(
                        {"selector": descriptor}, declared_path
                    )
                )
    targeted_green = _acceptance_test_targeted_green(context, declared_path)
    test_exists = _acceptance_test_exists_at_merge(context, declared_path)
    ran_green = _acceptance_test_ran_green_at_merge(context, declared_path)
    # The backend's own tri-state tree check (True/False/None); anything but a
    # bool means "never determinately corroborated" and is passed through as
    # None so the proven mint for a not-in-diff test stays capped soft.
    _corroborated_raw = (context or {}).get(ACCEPTANCE_TEST_CORROBORATED_KEY)
    corroborated = _corroborated_raw if isinstance(_corroborated_raw, bool) else None

    if evidence.kind == "named_test":
        return _evaluate_named_test(
            evidence,
            changed,
            verified,
            changed_file_provenance_available,
            truncated,
            parent_derived=parent_derived_files,
            targeted_green=targeted_green,
            test_exists=test_exists,
            ran_green=ran_green,
            block_describes_other_test=block_describes_other_test,
            corroborated=corroborated,
        )

    if evidence.kind == "downstream_artifact":
        return _evaluate_downstream_artifact(evidence, changed)

    if evidence.kind == "visual":
        return _evaluate_visual(project_id, context, evidence)

    if evidence.kind == "coverage_delta":
        return _evaluate_coverage_delta(evidence, context)

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


# These are the durable evaluator inputs; verdict and audit metadata are excluded.
_ACCEPTANCE_INPUT_CONTEXT_KEYS = (
    "_pr_changed_files",
    "_pr_changed_files_truncated",
    PARENT_DERIVED_CHANGED_FILES_KEY,
    COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY,
    "_pr_changed_files_provenance",
    PR_CHANGED_FILES_STAMP_SKIPPED_KEY,
    ACCEPTANCE_TEST_CORROBORATED_KEY,
    TEST_COVERAGE_RESULT_KEY,
    "viewport_failure_screenshot_uri",
    INHERITED_ACCEPTANCE_BLOCK_KEY,
)
_ACCEPTANCE_INPUT_REVIEW_LOOP_KEYS = (
    UNVERIFIED_MERGE_REASON_KEY,
    MERGE_ACCEPTANCE_BLOCK_KEY,
    ACCEPTANCE_TEST_TARGETED_GREEN_KEY,
    ACCEPTANCE_TEST_TARGETED_DESCRIPTOR_KEY,
)
_ACCEPTANCE_TEST_BLOCK_INPUT_KEYS = (
    "present", "ran", "status", "passed", "selector", "descriptor"
)


def _fingerprint_mapping(value: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        key: value[key]
        for key in keys
        if key in value
    }


def acceptance_input_fingerprint(
    payload: dict | None,
    context: Mapping[str, Any] | None,
    project_id: object,
) -> str:
    """Hash evaluator inputs so late evidence can invalidate a current stamp."""
    source = context or {}
    relevant_context: dict[str, Any] = {}
    for key in _ACCEPTANCE_INPUT_CONTEXT_KEYS:
        if key not in source:
            continue
        value = source[key]
        if key in (INHERITED_ACCEPTANCE_BLOCK_KEY, TEST_COVERAGE_RESULT_KEY):
            keys = (
                ("coverage_pct", "source")
                if key == TEST_COVERAGE_RESULT_KEY
                else _ACCEPTANCE_TEST_BLOCK_INPUT_KEYS
            )
            value = _fingerprint_mapping(value, keys)
            if value is None:
                continue
        elif key == ACCEPTANCE_TEST_CORROBORATED_KEY:
            if not isinstance(value, bool):
                continue
        elif key in (
            "_pr_changed_files_truncated",
            PARENT_DERIVED_CHANGED_FILES_KEY,
            COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY,
        ):
            value = bool(value)
        relevant_context[key] = value
    review_loop = source.get(REVIEW_LOOP_KEY)
    if isinstance(review_loop, Mapping):
        projected_review_loop: dict[str, Any] = {}
        for key in _ACCEPTANCE_INPUT_REVIEW_LOOP_KEYS:
            if key not in review_loop:
                continue
            value = review_loop[key]
            if key == MERGE_ACCEPTANCE_BLOCK_KEY:
                value = _fingerprint_mapping(value, _ACCEPTANCE_TEST_BLOCK_INPUT_KEYS)
                if value is None:
                    continue
            projected_review_loop[key] = value
        if projected_review_loop:
            relevant_context[REVIEW_LOOP_KEY] = projected_review_loop
    material = {
        "payload": payload,
        "project_id": str(project_id),
        "context": relevant_context,
    }
    canonical = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_code_acceptance(task: Task) -> AcceptanceGateVerdict:
    """Evaluate ``task``'s declared acceptance evidence, fail-closed.

    Thin ``Task`` adapter over the pure :func:`evaluate_acceptance` core: reads
    the declaration from the payload, the PR's changed files from
    ``context['_pr_changed_files']``, and whether the merge was test-verified
    from the no-CI gate's recorded reason. A task with no declaration returns a
    non-recorded passing verdict (``declared=False``), preserving today's
    behavior exactly.
    """
    return evaluate_acceptance(task.first_payload, task.context, task.project_id)


def evaluate_premerge_acceptance(task: Task) -> AcceptanceGateVerdict | None:
    """Evaluate the structurally-checkable half of ``task``'s declared acceptance
    evidence while the PR is still open.

    Mirrors :func:`evaluate_code_acceptance` but only judges what is decidable
    from the declaration + the PR's changed files before merge: named_test is
    satisfied when the diff ships a non-test, semantically-grounded seam for it
    to exercise — a declared test absent from the diff is NOT a defect, because
    the merge gate credits an unmodified test that exists at the merge commit
    and ran green (existence-at-merge and the green run are verified at merge);
    downstream_artifact is delivered by the diff; visual / coverage_delta
    declarations are guaranteed well-formed by the schema (the screenshot /
    measurement is verified at merge); observable_behavior ships a real diff. A
    structural defect (test-only diff, ungrounded seam, undelivered artifact)
    returns ``satisfied=False`` with the same actionable reason string the
    merge-time gate uses (:func:`classify_unsatisfied_reason` maps it
    identically); a task with no declaration returns ``None``.
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
        # Stamps the rule version that produced this verdict so a later
        # qualification can tell which recorded rows were judged by a retired
        # rule and re-derive them (design doc c50453ef). Consumed by
        # :func:`effective_acceptance_verdict`.
        "rule_version": ACCEPTANCE_RULE_VERSION,
        "input_fingerprint": acceptance_input_fingerprint(
            task.first_payload, task.context, task.project_id
        ),
        # Audit honesty (design doc c50453ef §5): this is the ORIGINAL recording
        # of the verdict at land time, not a re-derivation. A re-derivation
        # (rule-version bump) sets this to False and nests the original verdict
        # under ``land_time_verdict`` so the land-time judgment is never
        # destroyed. Rollup propagation records a child's verdict here too — the
        # child's own first recording, stamped against its merge record.
        "stamped_at_land_time": True,
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


def effective_acceptance_verdict(task: Task) -> dict | None:
    """The verdict a landed task's acceptance evidence carries under the CURRENT rule.

    Rung 3 of design doc c50453ef: the qualifier's acceptance-window tally must
    not trust a verdict frozen under a RETIRED rule — an evidence-rule improvement
    landing mid-version should re-score work that was already merged under the old
    rule. Returns the recorded :data:`CODE_ACCEPTANCE_VERDICT_KEY` dict verbatim
    when it was stamped by the current :data:`ACCEPTANCE_RULE_VERSION`; otherwise
    re-derives it from the task's persisted merge record via the pure
    :func:`evaluate_acceptance` core (same payload/context/project_id the live
    gate used — no agent re-run, no network) and re-stamps the current rule.

    Fail-safes (both keep a stale recorded row stable instead of silently
    re-measuring it):

      * No recorded verdict (undeclared / legacy) returns ``None``, so the
        caller keeps today's undeclared / inferred-test-cochange classification.
      * A re-derivation that comes back ``declared=False`` (the payload no longer
        declares evidence — declaration removed after stamping) returns the frozen
        dict verbatim rather than erasing a recorded verdict.
      * A consumed ``downstream_artifact`` verdict is never downgraded: the
        consumed upgrade (``consumed`` / ``consumed_by_task_id`` /
        ``consumed_artifact_id``, ``proven=True``) applied by
        :func:`record_artifact_consumptions_for_task` lives outside the pure core
        (it needs the consumption rows), so re-derivation would otherwise flip a
        consumed verdict back to satisfied-but-unproven. The re-derived dict keeps
        ``proven=True`` whenever the frozen dict was ``consumed``.

    Pure and side-effect-free (does not mutate ``task.context``); the caller
    decides whether a corrected verdict is written anywhere.
    """
    frozen = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    if not isinstance(frozen, dict):
        return None
    current_fingerprint = acceptance_input_fingerprint(
        task.first_payload, task.context, task.project_id
    )
    if (
        frozen.get("rule_version") == ACCEPTANCE_RULE_VERSION
        and frozen.get("input_fingerprint") == current_fingerprint
    ):
        # Stamped by the current rule against these exact inputs — nothing to
        # re-derive. Missing fingerprints are legacy rows and are healed below.
        return frozen
    rederived = evaluate_acceptance(task.first_payload, task.context, task.project_id)
    if not rederived.declared:
        # The payload no longer declares acceptance evidence; keep the recorded
        # row stable rather than erasing it into the undeclared bucket.
        return frozen
    effective = dict(frozen)
    # Audit honesty (design doc c50453ef §5): the land-time verdict is NEVER
    # destroyed by re-derivation. Nest the ORIGINAL land-time recording — the
    # frozen dict itself when it is the original stamp, else the already-nested
    # original from a prior re-derivation (never a re-derived chain). A reviewer
    # can always read what the retiring rule judged, side by side with what the
    # current rule re-derives.
    original_land_time = (
        frozen
        if frozen.get("stamped_at_land_time") is not False
        else frozen.get("land_time_verdict") or frozen
    )
    effective.update(
        {
            "satisfied": rederived.satisfied,
            "proven": rederived.proven,
            "kind": rederived.kind,
            "reason": rederived.reason,
            "proof_ref": rederived.proof_ref,
            "rule_version": ACCEPTANCE_RULE_VERSION,
            "input_fingerprint": current_fingerprint,
            "stamped_at_land_time": False,
            "rederived_at": _utcnow().isoformat(),
            "rederived_by": REDERIVED_BY_QUALIFIER,
            "land_time_verdict": original_land_time,
        }
    )
    if frozen.get("consumed") is True:
        # Preserve the consumption upgrade: consumed ⊂ proven, so a consumed
        # verdict stays proven even though the pure core cannot see consumption.
        effective["proven"] = True
    return effective


def verdict_rule_version(recorded: Mapping[str, Any] | None) -> int:
    """The rule version a recorded verdict was stamped under, ``0`` when absent.

    Used by the qualifier's window audit (:data:`ACCEPTANCE_AGGREGATE_BUCKETS`)
    to count how many recorded rows were judged by each RETIRED version before
    re-derivation. ``0`` means the row predates the version stamp — a legacy
    verdict recorded before design doc c50453ef landed.
    """
    if not isinstance(recorded, dict):
        return 0
    version = recorded.get("rule_version")
    return version if isinstance(version, int) else 0


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


def finding_clearance_blocks_success(context: Mapping[str, Any] | None) -> bool:
    """Whether a merged finding-clearing claim lacks authoritative proof.

    ``resolving_finding_keys`` is an explicit claim that a write task addresses
    carried audit findings. It is deliberately insufficient on its own: a PR
    merge proves delivery, not that the findings disappeared from the current
    tree. Credit is allowed only when a marker covers every claimed key and its
    ``verified_against_sha`` equals the task's exact landed commit. Missing,
    malformed, partial, or stale markers fail closed.
    """
    ctx = context if isinstance(context, Mapping) else {}
    raw_keys = ctx.get("resolving_finding_keys")
    if not isinstance(raw_keys, list):
        return False
    finding_keys = {
        key.strip() for key in raw_keys if isinstance(key, str) and key.strip()
    }
    if not finding_keys:
        return False

    verification = ctx.get(FINDING_CLEARANCE_VERIFICATION_KEY)
    if not isinstance(verification, Mapping):
        return True
    verified_keys = verification.get("finding_keys")
    if not isinstance(verified_keys, list):
        return True
    verified_key_set = {
        key.strip()
        for key in verified_keys
        if isinstance(key, str) and key.strip()
    }
    landed_sha = ctx.get(LANDED_MERGE_SHA_KEY)
    verified_sha = verification.get("verified_against_sha")
    return not (
        verification.get("state") == "verified"
        and finding_keys.issubset(verified_key_set)
        and isinstance(landed_sha, str)
        and bool(landed_sha.strip())
        and isinstance(verified_sha, str)
        and verified_sha.strip() == landed_sha.strip()
    )


def _claimed_finding_keys(context: Mapping[str, Any] | None) -> set[str]:
    """Normalize a task's ``resolving_finding_keys`` claim into a key set."""
    ctx = context if isinstance(context, Mapping) else {}
    raw_keys = ctx.get("resolving_finding_keys")
    if not isinstance(raw_keys, list):
        return set()
    return {key.strip() for key in raw_keys if isinstance(key, str) and key.strip()}


async def verify_finding_clearance_at_landing(
    task: Task,
    session: Any,
    *,
    merge_commit_sha: str | None = None,
    github_credentials: tuple[str, str, str] | None = None,
) -> dict[str, Any] | None:
    """Stamp the current-tree proof behind a merged finding-clearing claim.

    This is the producer :func:`finding_clearance_blocks_success` consumes. It
    replaces the old merge-time auto-resolve, which credited the *claim*
    (``resolving_finding_keys``) and never looked at the tree. Here each claimed
    finding is re-checked against the exact landed commit using the same
    identity the qualifier's audited-surface map records: a finding is cleared
    only when the blob it was graded against is provably no longer at its cited
    path (changed blob SHA, or the file deleted).

    Every unavailable input — no open version, truncated finding list, missing
    ``blob_sha``, unreadable path, no landed sha — leaves the marker
    ``unverified``, which the gate reads as "no credit". Nothing here can turn
    an unverified claim into credit, and nothing here can abort a landing:
    callers wrap it, and the marker is additive context.

    Returns the stamped marker, or ``None`` when the task claims no findings.
    """
    finding_keys = _claimed_finding_keys(getattr(task, "context", None))
    if not finding_keys:
        return None

    sha = merge_commit_sha.strip()[:64] if isinstance(merge_commit_sha, str) else ""
    marker: dict[str, Any] = {
        "state": "unverified",
        "finding_keys": [],
        "claimed_finding_keys": sorted(finding_keys),
        "verified_against_sha": sha or None,
        "checked_at": _utcnow().isoformat(),
    }

    def _stamp(reason: str | None = None) -> dict[str, Any]:
        if reason:
            marker["reason"] = reason
        context = dict(getattr(task, "context", None) or {})
        context[FINDING_CLEARANCE_VERIFICATION_KEY] = marker
        task.context = context
        return marker

    if not sha:
        return _stamp("landed commit sha unavailable")

    from app.release_version import current_open_version

    version = await current_open_version(session, task.project_id)
    if version is None:
        return _stamp("no open release version")

    from app.release_readiness import _latest_carried_audit_findings

    carried, truncated, _provenance = await _latest_carried_audit_findings(
        session, version
    )
    if truncated:
        # A clipped list cannot prove a claimed key is absent from it.
        return _stamp("carried finding list truncated")
    by_key = {
        str(finding.get("key")): finding
        for finding in carried
        if isinstance(finding, dict) and finding.get("key")
    }

    credentials = github_credentials
    if credentials is None:
        try:
            from app.routers.agent import get_github_token_for_task

            credentials = await get_github_token_for_task(task.id, session)
        except Exception:  # verification is never a landing gate
            logger.warning(
                "finding-clearance credentials unavailable for landed task %s",
                getattr(task, "id", None),
                exc_info=True,
            )
            credentials = None
    if (
        not isinstance(credentials, tuple)
        or len(credentials) != 3
        or not all(isinstance(value, str) and value.strip() for value in credentials)
    ):
        return _stamp("github credentials unavailable")

    from app.github import get_file_blob_sha_at_ref

    token, owner, repo = credentials
    verified: list[str] = []
    for key in sorted(finding_keys):
        finding = by_key.get(key)
        if not isinstance(finding, dict):
            continue
        path = str(finding.get("file") or finding.get("path") or "").strip()
        recorded_blob = finding.get("blob_sha")
        if not path or not isinstance(recorded_blob, str) or not recorded_blob.strip():
            # A cross-cutting finding with no single blob has no current-tree
            # identity to check; it cannot be machine-cleared.
            continue
        exists, current_blob = await get_file_blob_sha_at_ref(
            path,
            token=token,
            owner=owner,
            repo=repo,
            ref=sha,
            project_id=str(task.project_id) if task.project_id else None,
        )
        if exists is False:
            verified.append(key)
            continue
        if exists is True and current_blob and current_blob != recorded_blob.strip():
            verified.append(key)

    marker["finding_keys"] = verified
    if set(verified) >= finding_keys:
        marker["state"] = "verified"
        return _stamp()
    return _stamp("claimed findings still present on the landed tree")


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
    "named_test_diff_provenance_unavailable",
    "named_test_missing_from_diff",
    "named_test_not_run_green",
    "named_test_test_only_diff",
    "named_test_ungrounded_seam",
    "named_test_block_describes_other_test",
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
    green run, that shipped no production seam, that is not semantically
    grounded, or whose merge-test verdict describes a DIFFERENT test than the
    declared path; an artifact the diff never delivered; a malformed or missing
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
        if "describes a DIFFERENT test" in reason_str:
            return "named_test_block_describes_other_test"
        if "changed-file provenance was unavailable" in reason_str:
            return "named_test_diff_provenance_unavailable"
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
            except Exception:
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
    aggregate: dict[str, Any] = dict.fromkeys(ACCEPTANCE_AGGREGATE_BUCKETS, 0)
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


async def _corroborate_soft_named_test(task: Task, session: Any) -> bool:
    """Corroborate a soft named_test verdict that lacks a corroboration marker.

    Soft (``satisfied=True, proven=False``) named_test verdicts recorded on a
    non-CI-monitor landing path (rollup propagation, PR verification, recovery
    strands) never had :data:`ACCEPTANCE_TEST_CORROBORATED_KEY` stamped, so a
    not-in-diff declared test is capped at soft/unproven credit forever — the
    mechanical reason the acceptance window mints ~0 proven coverage. This runs
    the backend's own tree check against the task's stored merge commit and
    stamps a confirmed-PRESENCE marker so the caller's re-evaluation can mint
    machine-proven credit.

    Conservative: only a corroborated ``True`` stamps the marker; a corroborated
    ABSENCE or an indeterminate result leaves the marker absent so the recorded
    soft verdict is never downgraded (matching the backfill's upgrade-only
    contract). Best-effort — never raises.

    Returns True iff a confirmed-presence marker was newly stamped.
    """
    ctx = task.context or {}
    sha = ctx.get(LANDED_MERGE_SHA_KEY)
    if not isinstance(sha, str) or not sha:
        return False
    project = (
        await session.get(Project, task.project_id)
        if task.project_id is not None
        else None
    )
    try:
        result = await corroborate_acceptance_test_at_merge(
            task, session, project=project, commit_sha=sha[:64]
        )
    except Exception:
        logger.warning(
            "backfill corroboration failed for task %s",
            getattr(task, "id", None),
            exc_info=True,
        )
        return False
    if result is True:
        task.context = {
            **(task.context or {}),
            ACCEPTANCE_TEST_CORROBORATED_KEY: True,
        }
        return True
    return False


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
    3. ``satisfied=False`` with either a missing-diff or unavailable-provenance
       reason — recorded when changed-file provenance was absent or late
       (rollup children merge via the PARENT's single PR, so a verdict stamped
       before the parent's ``_pr_changed_files`` arrived cannot distinguish
       omission from missing data). Late-arriving provenance flips these to
       satisfied (proven when the task's own PR files are GitHub-verified, else
       capped at satisfied_unproven for the parent-derived union diff).
       Candidates of this class are re-admitted to the sweep once changed-file
       provenance has arrived — even when an OLD run already stamped the
       one-shot ``_backfill_checked_at`` marker on them — and leave the window
       terminally (``_backfill_missing_diff_terminal_at``) when provenance was
       in hand yet re-evaluation still failed. Fresh provenance clears both
       markers, so late re-stamps always re-admit the task without the class
       ever monopolizing the LIMIT-200 window.

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
        # missing-diff and unavailable-provenance classes.
        _missing_from_diff_verdict = or_(
            func.json_extract(
                Task.context, "$.code_acceptance_verdict.reason"
            ).like("%is not among the PR's changed files%"),
            func.json_extract(
                Task.context, "$.code_acceptance_verdict.reason"
            ).like("%changed-file provenance was unavailable%"),
        )
        # Re-admission gate for the missing-diff/unavailable-provenance classes:
        # changed-file
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
        _missing_from_diff_verdict = or_(
            Task.context["code_acceptance_verdict"]["reason"].astext.like(
                "%is not among the PR's changed files%"
            ),
            Task.context["code_acceptance_verdict"]["reason"].astext.like(
                "%changed-file provenance was unavailable%"
            ),
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
    # persisted verdict is a missing-diff/unavailable-provenance class AND whose changed-file
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
            except Exception:  # noqa: BLE001
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
                # Soft named_test verdicts (satisfied, unproven) recorded on a
                # non-CI-monitor landing path carry no corroboration marker, so a
                # not-in-diff declared test is credited soft forever. Corroborate
                # it against the stored merge commit before re-evaluating — a
                # test the backend confirms EXISTS at the merge commit then mints
                # machine-proven credit. Conservative: only a confirmed presence
                # stamps the marker; a corroborated absence / indeterminate
                # result leaves the soft credit untouched (backfill never
                # downgrades a recorded verdict).
                if ACCEPTANCE_TEST_CORROBORATED_KEY not in (task.context or {}):
                    await _corroborate_soft_named_test(task, session)
            elif satisfied is False:
                # Two re-evaluable unsatisfied classes:
                #   (a) Pre-fix verified-merge mis-scoring: the old
                #       _merge_was_test_verified treated an absent/malformed
                #       review_loop as UNVERIFIED, so a landed task whose context
                #       simply lacked a review_loop dict was recorded unsatisfied
                #       with the "did not run green" reason even though the merge
                #       was genuinely verified.
                #   (b) Missing diff provenance/omission: the declared named_test
                #       was not found in the recorded changed-file list, or no
                #       list was recorded — often because rollup children never
                #       owned a PR and the parent's changed files arrived LATE.
                #       Late-arriving provenance can flip this class, so it is
                #       admitted to re-evaluation; when still unprovable it is
                #       marked (terminally so when provenance was already in hand)
                #       and re-admitted only when fresh provenance clears the
                #       markers (see the ``missing_from_diff`` handling below).
                reason = verdict_data.get("reason")
                reason_str = reason if isinstance(reason, str) else ""
                missing_from_diff = (
                    "is not among the PR's changed files" in reason_str
                    or "changed-file provenance was unavailable" in reason_str
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
                    "rule_version": ACCEPTANCE_RULE_VERSION,
                    "input_fingerprint": acceptance_input_fingerprint(
                        task.first_payload, ctx, task.project_id
                    ),
                }
                task.context = ctx
                session.add(task)
                # Commit the verdict upgrade FIRST so a rollback inside
                # record_evidence (IntegrityError guard) does not undo it.
                await session.commit()

                from app.acceptance_criteria import record_task_merge_evidence
                try:
                    await record_task_merge_evidence(session, task, task.project_id)
                except Exception:
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
                        "rule_version": ACCEPTANCE_RULE_VERSION,
                        "input_fingerprint": acceptance_input_fingerprint(
                            task.first_payload, ctx, task.project_id
                        ),
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
