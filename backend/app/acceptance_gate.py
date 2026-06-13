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

import uuid
import operator
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from app.ci_resolution import is_degraded_unverified_reason
from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY
from app.landed_signal import merge_verification_status
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.artifact_prerequisites import ARTIFACT_PREREQUISITES_KEY
from app.artifact_prerequisites import downstream_artifact_logical_key
from app.models import Artifact
from app.models import ArtifactConsumption
from app.models import Task
from app.models import TaskStatus
from app.payload_schemas import (
    AcceptanceEvidence,
    _looks_like_test_file,
    read_acceptance_evidence,
)
from app.viewport_evidence import resolve_visual_evidence

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

ACCEPTANCE_AGGREGATE_BUCKETS: tuple[str, ...] = (
    "proven",
    "satisfied_unproven",
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
        proven=True,
        proof_ref=evidence.test_path or declared_path or None,
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

    if evidence.kind == "downstream_artifact":
        return _evaluate_downstream_artifact(evidence, changed)

    if evidence.kind == "visual":
        return _evaluate_visual(task, evidence)

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
        return "undeclared"
    if acceptance_blocks_success(recorded):
        return "declared_unsatisfied"
    if recorded.get("satisfied") is True:
        if acceptance_is_proven(recorded):
            return "proven"
        return "satisfied_unproven"
    return "malformed"


def acceptance_counts_as_verified_context(
    context: Mapping[str, Any] | None,
) -> bool:
    """Whether a landed task context still counts as verified merged work.

    Verified merges include the strict proven, soft satisfied-unproven, and
    undeclared buckets. Degraded, explicitly unsatisfied, and malformed rows do
    not count.
    """
    return classify_merged_acceptance_context(context) in {
        "proven",
        "satisfied_unproven",
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
    recorded: list[ArtifactConsumption] = []
    recorded_id_strings: list[str] = []

    for artifact_id in ids:
        artifact = artifacts_by_id.get(artifact_id)
        if artifact is None:
            continue
        producer = await session.get(Task, artifact.task_id)
        if producer is None or producer.project_id != task.project_id:
            continue
        existing = (
            (
                await session.execute(
                    select(ArtifactConsumption).where(
                        ArtifactConsumption.consumer_task_id == task.id,
                        ArtifactConsumption.artifact_id == artifact.id,
                    )
                )
            )
            .scalars()
            .first()
        )
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
