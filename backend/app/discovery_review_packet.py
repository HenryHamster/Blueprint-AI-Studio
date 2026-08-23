"""Semantic outcome-receipt construction for discovery decisions.

The decision controller consumes one bounded parent-level receipt rather than a
bag of child task rows.  Parallel fan-out therefore changes receipt counters,
not whether the controller is allowed to decide.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Mapping, Sequence
import uuid

from sqlmodel import select

from app.findings_index import (
    FINALIZED_RESEARCH_FINDINGS_KEY,
    finalized_research_findings,
)
from app.landed_signal import has_landed_signal
from app.models import (
    Artifact,
    ArtifactType,
    DiscoveryCycle,
    DiscoveryEvidenceItem,
    KnowledgeDocument,
    Task,
    TaskStatus,
)
from app.task_capabilities import WRITE_TASK_TYPES
from app.time_utils import _utcnow


DISCOVERY_REVIEW_PACKET_VERSION = 3
DISCOVERY_REVIEW_MAX_PACKET_ITEMS = 24
DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS = 32_000
DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS = 8_000
DISCOVERY_REVIEW_URI_CHARS = 1_000
DISCOVERY_REVIEW_BLOCKED_GRACE = timedelta(hours=2)
DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS = 3
DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS = 2_000
# The smallest per-item slice of the digest budget that still reads as a
# representation of that item rather than a stub. An omitted item is
# "covered" when it is either reproduced in full or reproduced at no less
# than this; only a cohort so numerous that even this floor cannot be paid
# for every item leaves an item genuinely unaccounted for.
DISCOVERY_REVIEW_SEMANTIC_DIGEST_MIN_ITEM_CHARS = 40
# Which top-level snapshot keys carry decision authority -- the content the
# reviewer must hold whole to bind advance/park/stop instead of handing the
# question back to a human. Everything outside these sets is framing the
# packet supplies by other means, so shortening it costs legibility, never
# authority. See _bounded_decision_snapshot for why that distinction exists.
#
# The bet set is exactly the successor bet contract (``_NEXT_BET_KEYS`` in
# discovery_review.py). It is restated here rather than imported because
# discovery_review imports this module, and it is pinned equal to that
# contract by test_discovery_review_packet_budget.py so the two cannot drift.
#   * ``claim`` and ``decision_question`` are the proposition under judgement
#     and the literal question the verdict answers; a cut changes what was
#     asked.
#   * ``success_evidence`` and ``retirement_condition`` are the bars for "it
#     worked" and "stop", pre-registered before the evidence existed. Scoring
#     evidence against a truncated bar is exactly the authority loss this gate
#     is for.
#   * ``mode`` (research/measure/build) decides which evidence even counts. It
#     is one short token and never at risk; it is listed for completeness.
# Deliberately excluded from the bet set:
#   * ``direction`` -- direction_authority_snapshot(...), and the field that
#     actually overflows, because it carries ``research_context``, the
#     accumulated outcome memory research completion rewrites and grows every
#     generation. Its identifying text reaches this reviewer twice over
#     without it: the generation-one ``claim`` is the commissioned question
#     or, when there is none, ``direction["text"]`` itself bounded to 2,000
#     chars, and the receipt's ``source_parent.title`` is literally
#     ``"Research: {direction_text}"``. When that parent is absent
#     the packet already records missing required evidence on its own, so
#     there is no case in which shortening ``direction`` is the reviewer's
#     only loss. It is a mapping, so it is bounded field-aware in turn and its
#     own proof contract (``context.success_evidence``,
#     ``context.retirement_condition``) survives even when its prose does not.
#   * ``alternatives`` -- a list of at most 12 options the review *may*
#     propose. No backend code reads it; ``_normalize_next_bet`` does not
#     constrain an ``advance`` to it. Losing options narrows imagination, not
#     authority.
DISCOVERY_REVIEW_BET_AUTHORITY_KEYS = frozenset(
    {
        "mode",
        "claim",
        "decision_question",
        "success_evidence",
        "retirement_condition",
    }
)
# The intent snapshot (built in discovery_admission.ensure_discovery_cycle_for_research)
# is framing rather than contract, with two exceptions the verdict is measured
# against: the decision rule asks whether the work created *goal-relevant*
# progress, so ``release_goal`` and the operator-authored ``user_purpose`` are
# what "relevant" is judged against and must arrive whole. Excluded:
# ``project_name``, ``purpose_source``, ``problem_model_revision`` and
# ``release_version_id`` are identifiers and provenance, short enough to ride
# whole anyway and not judgement inputs; ``constraints`` and
# ``project_context`` are the unbounded operator-authored problem-model maps
# that overflow this budget, and they bind whoever builds the next bet rather
# than the verdict on this one; ``running_update_excerpt`` is already an
# excerpt, bounded to 2,000 chars at the source.
DISCOVERY_REVIEW_INTENT_AUTHORITY_KEYS = frozenset({"release_goal", "user_purpose"})
DISCOVERY_REVIEW_RESEARCH_DETAILS = 6
DISCOVERY_REVIEW_KNOWLEDGE_DETAILS = 6
DISCOVERY_REVIEW_LANDED_OUTCOMES = 4
DISCOVERY_REVIEW_EXPERIMENT_DETAILS = 6
DISCOVERY_REVIEW_VERIFICATION_DETAILS = 6
# Budget-pressure degradation must never render a category that truly has
# evidence as fully empty -- that is indistinguishable from "nothing
# happened" to the reviewer and is exactly the defect this floor guards
# against (see _repair_zeroed_categories below). Keep at least this many of
# the most recent/relevant items, prose-truncated if needed to fit.
DISCOVERY_REVIEW_MIN_CATEGORY_ITEMS = 3
# Successively more aggressive per-item prose caps tried while floor-repairing
# a zeroed category, so a handful of representative items can still be made
# to fit even when their full-length versions cannot.
DISCOVERY_REVIEW_FLOOR_PROSE_CHAR_LEVELS = (300, 120, 40)

_VERIFICATION_TASK_TYPES = frozenset(
    {
        "review",
        "qa",
        "smoketest",
        "verify_tests",
        "perf",
        "security_audit",
        "compute_experiment",
        "walkthrough_e2e",
    }
)
_EXPERIMENT_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.COMPUTE_RESULT.value,
        ArtifactType.PERF_REPORT.value,
        ArtifactType.SMOKETEST_RESULT.value,
        ArtifactType.WALKTHROUGH_E2E_RESULT.value,
        ArtifactType.WALKTHROUGH_E2E_REPORT.value,
    }
)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return str(_enum_value(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_cost(value: Any) -> int:
    """Exact characters ``value`` contributes to a canonical JSON rendering."""

    return len(_canonical_json(value))


def _snapshot_value_text(value: Any) -> str:
    """The prose form of a snapshot value, for when it has to be shortened.

    Strings are shortened as themselves rather than as their quoted JSON so a
    cut ``claim`` still reads as a sentence; everything else is shortened as
    its canonical JSON so structure stays visible.
    """

    return value if isinstance(value, str) else _canonical_json(value)


def _fit_json_text(text: str, budget: int) -> str:
    """Longest ellipsis-marked prefix of ``text`` that *renders* inside ``budget``.

    The budget is spent on the rendered form, not the raw one. JSON escaping
    expands quotes, backslashes and control characters, so a raw-character
    prefix is not a rendered-character bound: the pre-fix code cut a
    canonical-JSON prefix at exactly the cap and then re-encoded that prefix
    as a JSON string, which re-escaped every quote it had just copied, so the
    emitted snapshot could exceed the very cap it was cut to. Binary search on
    the prefix length is exact because escaping is per-character, which makes
    the rendered cost monotonic in the prefix length.
    """

    if _json_cost(text) <= budget:
        return text
    if budget < _json_cost("…"):
        # Not even a bare marker fits. The key still has to be present, so
        # emit the empty string; the caller has already reserved its 2 chars.
        return ""
    low, high, best = 0, len(text), "…"
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid] + "…"
        if _json_cost(candidate) <= budget:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    return best


def _fit_snapshot_value(value: Any, budget: int, *, depth: int) -> tuple[Any, bool]:
    """Bound one snapshot value to ``budget`` rendered chars.

    Returns the bounded value and whether it was carried whole. A mapping
    that does not fit is bounded field-aware in turn rather than flattened to
    a prefix, so a nested authority envelope keeps all of its own keys -- this
    is what preserves ``direction.context`` (the generation-one proof
    contract: success_evidence, retirement_condition,
    implementation_constraints) even when the direction's prose cannot fit.
    """

    if _json_cost(value) <= budget:
        return value, True
    if depth > 0 and isinstance(value, Mapping):
        nested = _fit_mapping(value, budget, depth=depth - 1)
        if nested is not None:
            return nested[0], False
    if depth > 0 and isinstance(value, list):
        # Same reason as the mapping branch, one container down: a list that
        # does not fit keeps its items and its length rather than collapsing
        # into a JSON prefix that stops mid-element. A bet's ``alternatives``
        # is the shape this exists for -- twelve options the reviewer weighs
        # against the claim, of which a prefix cut would silently show four.
        items = _fit_sequence(value, budget, depth=depth - 1)
        if items is not None:
            return items, False
    return _fit_json_text(_snapshot_value_text(value), budget), False


def _fit_sequence(
    source: list[Any], budget: int, *, depth: int
) -> list[Any] | None:
    """Bound a list into ``budget`` rendered chars while keeping every item.

    Returns ``None`` when the brackets, commas and one empty string per item
    already exceed the budget, in which case no item-wise form exists at this
    size and the caller falls back to a prefix of the rendered list.
    """

    items = list(source)
    if not items:
        return [] if budget >= 2 else None
    # Exact frame cost: the two brackets and one comma between consecutive
    # items. The floor is the two quotes each item needs to render as at
    # least the empty string.
    frame = 2 + len(items) - 1
    floor = 2 * len(items)
    if frame + floor > budget:
        return None
    wants = [max(0, _json_cost(item) - 2) for item in items]
    return [
        _fit_snapshot_value(item, 2 + share, depth=depth)[0]
        for item, share in zip(
            items,
            _fair_share_digest_budgets(wants, budget - frame - floor),
            strict=True,
        )
    ]


def _fit_mapping(
    source: Mapping[str, Any],
    budget: int,
    *,
    authority_keys: frozenset[str] = frozenset(),
    depth: int = 1,
) -> tuple[dict[str, Any], list[str], list[str]] | None:
    """Bound ``source`` into ``budget`` rendered chars while keeping every key.

    Returns the bounded mapping, the keys that could not be carried whole,
    and the subset of those cut below the legibility floor -- or ``None`` when
    the key names plus one empty-string value each already exceed the budget,
    in which case no field-aware form exists at this size at all.

    Budget is spent decision-first: the keys in ``authority_keys`` are served
    from the whole budget by the same max-min fair split the omitted-evidence
    digest uses, and only what they leave behind is shared out among the rest.
    That ordering is the point of the function. Canonical JSON sorts keys, so
    a flat prefix cut spends the budget alphabetically: a long ``direction``
    or ``alternatives`` silently consumed ``mode``, ``retirement_condition``
    and ``success_evidence``, which is why the human-facing render of the
    stranded d-f502e1a5 bet showed ``mode=?``.
    """

    normalized = {str(key): value for key, value in source.items()}
    keys = sorted(normalized)
    if not keys:
        return ({}, [], []) if budget >= 2 else None
    # Exact frame cost: the two braces, one quoted key plus its colon per
    # entry, and one comma between consecutive entries.
    frame = 2 + sum(_json_cost(key) + 1 for key in keys) + len(keys) - 1
    # Every value must render as at least the empty string, or its key cannot
    # be present at all -- and keeping every key present is the whole contract
    # this function exists to honour.
    floor = 2 * len(keys)
    if frame + floor > budget:
        return None
    spendable = budget - frame - floor
    wants = {key: max(0, _json_cost(normalized[key]) - 2) for key in keys}
    bearing = [key for key in keys if key in authority_keys]
    ancillary = [key for key in keys if key not in authority_keys]
    shares: dict[str, int] = {}
    bearing_shares = _fair_share_digest_budgets(
        [wants[key] for key in bearing], spendable
    )
    shares.update(zip(bearing, bearing_shares, strict=True))
    shares.update(
        zip(
            ancillary,
            _fair_share_digest_budgets(
                [wants[key] for key in ancillary],
                spendable - sum(bearing_shares),
            ),
            strict=True,
        )
    )
    bounded: dict[str, Any] = {}
    shortened: list[str] = []
    elided: list[str] = []
    for key in keys:
        value_budget = 2 + shares[key]
        fitted, whole = _fit_snapshot_value(
            normalized[key], value_budget, depth=depth
        )
        bounded[key] = fitted
        if whole:
            continue
        shortened.append(key)
        if value_budget < DISCOVERY_REVIEW_SEMANTIC_DIGEST_MIN_ITEM_CHARS:
            # Below the shared legibility floor the value has stopped being a
            # shortened representation of the authored text and reads as a
            # placeholder, so report it as elided rather than merely cut.
            elided.append(key)
    return bounded, shortened, elided


def _bounded_decision_snapshot(
    value: Any, *, authority_keys: frozenset[str]
) -> tuple[dict[str, Any], bool, str | None]:
    """Bound one frozen intent/bet snapshot without throwing its shape away.

    Returns the bounded snapshot, whether anything had to be cut, and -- when
    a decision-bearing key could not be carried whole -- the names of the keys
    that were lost, for the caller's authority blocker.

    This used to render the snapshot to canonical JSON and, on overflow,
    discard the structured dict entirely in favour of an 8,000-character
    prefix of that JSON. Canonical JSON sorts keys, so the prefix cut spent
    the budget in alphabetical order: on a successor bet
    (claim, decision_question, direction, mode, retirement_condition,
    success_evidence) an accumulated ``direction`` -- which carries
    ``research_context``, the outcome memory research completion rewrites and
    grows every generation -- ate the bet's mode and both of its
    pre-registered bars. Worse, the resulting boolean was fed straight into
    ``authority_blockers``, which forced ``required_evidence_omitted`` and so
    left ``ask_operator`` as the only permitted verdict. Direction d-f502e1a5
    stranded at ``discovery_ask_operator_awaiting_resolution`` from
    2026-08-19 across generations four and five that way, its rendered bet
    degraded to ``mode=?``, asking a human for authority the engine was
    holding intact in its own database.

    The bound is now spent field-aware: every top-level key survives, short
    values ride whole, long values are shortened with an explicit ellipsis,
    and the keys that were cut are named in the emitted form. Authority is
    revoked only when a *decision-bearing* key could not be carried whole --
    never because an ancillary field had to be shortened.
    """

    rendered = _canonical_json(value or {})
    if len(rendered) <= DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS:
        decoded = json.loads(rendered)
        return (decoded if isinstance(decoded, dict) else {}), False, None
    checksum = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    decoded = json.loads(rendered)
    if isinstance(decoded, dict) and decoded:
        keys = sorted(decoded)
        # The honesty bookkeeping is reserved at its worst case -- every key
        # named in both lists -- before a single character of value budget is
        # handed out, so the emitted form is inside the cap by construction
        # and not by a trailing cut. (A source key colliding with one of these
        # names would be overwritten rather than dropped; the snapshot
        # builders in discovery_admission.py author none of them, and
        # over-reserving for a key that turns out not to be extra is safe in
        # the only direction that matters.)
        #
        # Past a few hundred keys that worst case is larger than the structure
        # it is protecting, so a second, compact form reports the same two
        # facts as counts. Naming four hundred shortened keys is not more
        # honest than counting them, and the alternative -- falling through to
        # the prefix excerpt, which revokes authority -- would be strictly
        # worse than either. The blocker string is computed from the key names
        # regardless of which form is emitted, so the operator is still told
        # exactly which decision-bearing field was lost.
        for names in (True, False):
            bookkeeping: dict[str, Any] = (
                {"snapshot_elided_keys": keys, "snapshot_shortened_keys": keys}
                if names
                else {
                    "snapshot_elided_key_count": len(keys),
                    "snapshot_shortened_key_count": len(keys),
                }
            )
            reserved = sum(
                1 + _json_cost(key) + 1 + _json_cost(sample)
                for key, sample in (
                    ("original_checksum", checksum),
                    ("snapshot_truncated", True),
                    *bookkeeping.items(),
                )
            )
            fitted = _fit_mapping(
                decoded,
                DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS - reserved,
                authority_keys=authority_keys,
                # Two levels of field-aware descent, which is what the
                # authored shapes need and no more: the bet's ``direction``
                # envelope is one level down and its proof contract
                # (``direction.context``) is two.
                depth=2,
            )
            if fitted is None:
                continue
            bounded, shortened, elided = fitted
            bounded.update(
                {
                    "original_checksum": checksum,
                    "snapshot_truncated": True,
                    **(
                        {
                            "snapshot_elided_keys": elided,
                            "snapshot_shortened_keys": shortened,
                        }
                        if names
                        else {
                            "snapshot_elided_key_count": len(elided),
                            "snapshot_shortened_key_count": len(shortened),
                        }
                    ),
                }
            )
            # Post-join clamp. The allocation above proves the bound, but
            # #17723 found a real bound breach in the sibling digest at high
            # cardinality, so measure the joined form rather than trusting the
            # arithmetic; a breach falls through to the excerpt form below,
            # which is bounded by construction on a single string.
            if _json_cost(bounded) > DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS:
                continue
            lost = sorted(set(shortened) & authority_keys)
            return bounded, True, (", ".join(lost) if lost else None)
    # Reached only when the snapshot is not a JSON object at all, or when its
    # key names alone -- before any value gets a single character -- cannot
    # fit the cap. No field-aware form exists at that size, so fall back to
    # the historical excerpt and say outright that the structure is gone.
    # Authority is revoked here, because what was lost is the shape the
    # reviewer decides from, not one shortened field inside it.
    fallback: dict[str, Any] = {
        "snapshot_truncated": True,
        "snapshot_structure_omitted": True,
        "excerpt": "",
        "original_checksum": checksum,
    }
    fallback["excerpt"] = _fit_json_text(
        rendered,
        DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS - (_json_cost(fallback) - 2),
    )
    return fallback, True, "the snapshot structure itself"


def _bounded_metadata(value: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    rendered = _canonical_json(value)
    if len(rendered) <= DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS:
        return json.loads(rendered), False
    return (
        {
            "excerpt": rendered[:DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS],
            "original_checksum": _checksum(value),
            "truncated": True,
        },
        True,
    )


def _receipt_fits_evidence_metadata(snapshot: Mapping[str, Any]) -> bool:
    return (
        len(
            _canonical_json(
                {
                    "packet_version": DISCOVERY_REVIEW_PACKET_VERSION,
                    "snapshot": snapshot,
                }
            )
        )
        <= DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS
    )


def _bounded_json_value(value: Any, *, max_chars: int) -> Any:
    rendered = _canonical_json(value)
    if len(rendered) <= max_chars:
        return json.loads(rendered)
    return {
        "semantic_excerpt": " ".join(rendered.split())[:max_chars],
        "original_checksum": _checksum(value),
        "truncated": True,
    }


def _bounded_missing_reasons(values: list[str]) -> list[str]:
    unique = list(dict.fromkeys(str(value)[:500] for value in values if str(value)))
    if len(unique) <= 20:
        return unique
    omitted = unique[20:]
    return [
        *unique[:20],
        (
            f"{len(omitted)} additional missing-evidence reason(s); "
            f"checksum={_checksum(omitted)}"
        ),
    ]


def _canonical_semantic_value(value: Any) -> str:
    rendered = (
        str(value)
        if isinstance(value, (str, int, float, bool))
        else _canonical_json(value)
    )
    return " ".join(rendered.split())


def _task_type(task: Task) -> str:
    return str(task.first_payload.get("type") or "")


def _pr_number(task: Task) -> int | str | None:
    context = task.context or {}
    review_loop = context.get("review_loop")
    if isinstance(review_loop, Mapping) and review_loop.get("pr_number") is not None:
        return review_loop.get("pr_number")
    for source in (context, task.first_payload):
        for key in ("rollup_pr_number", "pr_number"):
            if source.get(key) is not None:
                return source.get(key)
    return None


def _merge_sha(task: Task) -> str | None:
    context = task.context or {}
    for source in (context, task.first_payload):
        for key in (
            "merge_commit_sha",
            "merged_sha",
            "deployed_commit_sha",
            "serving_commit_sha",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                return value[:64]
    return None


def _artifact_type(artifact: Artifact) -> str:
    return str(_enum_value(artifact.artifact_type))


def _artifact_semantic_projection(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.id),
        "task_id": str(artifact.task_id),
        "type": _artifact_type(artifact),
        "logical_key": artifact.logical_key,
        "commit_hash": artifact.commit_hash,
        "repo_url": artifact.repo_url,
        "pr_url": artifact.pr_url,
        "storage_uri": artifact.storage_uri,
        "logs": artifact.logs,
    }


def _compact_artifact(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.id),
        "task_id": str(artifact.task_id),
        "type": _artifact_type(artifact),
        "logical_key": str(artifact.logical_key or "")[:300] or None,
        "commit_hash": artifact.commit_hash,
        "pr_url": str(artifact.pr_url or "")[:DISCOVERY_REVIEW_URI_CHARS] or None,
        "storage_uri": str(artifact.storage_uri or "")[:DISCOVERY_REVIEW_URI_CHARS]
        or None,
        "semantic_summary": _canonical_semantic_value(
            _artifact_semantic_projection(artifact)
        ),
    }


def _verification_summary(task: Task) -> dict[str, Any]:
    context = task.context or {}
    raw_acceptance_verdict = context.get("acceptance_verdict")
    raw_acceptance_evidence = context.get("acceptance_evidence_result")
    acceptance_verdict = _bounded_json_value(
        raw_acceptance_verdict, max_chars=500
    )
    acceptance_evidence = _bounded_json_value(
        raw_acceptance_evidence, max_chars=500
    )
    failure_category = str(context.get("failure_category") or "")[:240] or None
    latest_status = str(task.latest_status_message or "")[:800] or None
    semantic_projection = {
        key: value
        for key, value in {
            "acceptance_verdict": raw_acceptance_verdict,
            "acceptance_evidence_result": raw_acceptance_evidence,
            "failure_category": context.get("failure_category"),
            "latest_status": task.latest_status_message,
        }.items()
        if value is not None and value != ""
    }
    summary = {
        "task_id": str(task.id),
        "type": _task_type(task),
        "status": str(_enum_value(task.status)),
        "acceptance_verdict": acceptance_verdict,
        "acceptance_evidence_result": acceptance_evidence,
        "failure_category": failure_category,
        "latest_status": latest_status,
    }
    if semantic_projection:
        summary["semantic_summary"] = _canonical_semantic_value(
            semantic_projection
        )
    return summary


def _semantic_text(value: Mapping[str, Any]) -> str:
    finding = value.get("finding")
    semantic_source = (
        value
        if value.get("semantic_summary") is not None
        else finding
        if isinstance(finding, Mapping)
        else value
    )
    candidates = [
        semantic_source.get(key)
        for key in (
            "semantic_summary",
            "conclusion",
            "summary",
            "verdict",
            "result",
            "evidence",
            "latest_status",
            "description",
            "title",
            "finding",
        )
        if semantic_source.get(key) is not None
    ]
    excerpt_source = candidates[0] if candidates else semantic_source
    return _canonical_semantic_value(excerpt_source)


def _semantic_excerpt(value: Mapping[str, Any], *, max_chars: int) -> str:
    return _semantic_text(value)[:max_chars]


def _fair_share_digest_budgets(lengths: list[int], budget: int) -> list[int]:
    """Max-min fair split of ``budget`` characters across ``lengths``.

    Items that want less than an equal share are settled at exactly what they
    want and their leftover is redistributed to the items still over-budget,
    so a cohort of short entries plus one long one spends the whole budget on
    the long one rather than reserving idle room for entries that never
    needed it. Every returned share is non-negative and their sum never
    exceeds ``budget``.
    """

    shares = [0] * len(lengths)
    if budget <= 0 or not lengths:
        return shares
    remaining = budget
    unresolved = list(range(len(lengths)))
    while unresolved:
        share = remaining // len(unresolved)
        settled = [index for index in unresolved if lengths[index] <= share]
        if not settled:
            for index in unresolved:
                shares[index] = share
            break
        for index in settled:
            shares[index] = lengths[index]
            remaining -= lengths[index]
        settled_set = set(settled)
        unresolved = [index for index in unresolved if index not in settled_set]
    return shares


def _fit_semantic_digest(omitted: list[dict[str, Any]]) -> dict[str, Any]:
    """Represent *every* omitted item inside the fixed digest budget.

    This digest is the only field that covers all omitted items -- the
    ``omitted_semantic_index`` above samples just
    ``DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS`` of them -- so it is what stops
    an omitted item from reading to the reviewer as an absent one.

    It used to be built by joining every item's untruncated text and hard
    cutting the join at ``DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS``. That
    front-to-back spend had two production consequences, both seen live on
    2026-08-23 when five discovery directions stranded at ``ask_operator``
    with ``landed_outcomes:kept_1_of_8``:

    * one verbose entry pushed the tail entries out of the digest entirely,
      which is exactly the silent absence the field exists to prevent; and
    * the resulting "incomplete" flag revoked binding authority for the whole
      packet, so the engine asked a human to hand-supply evidence it was
      already holding in its own database.

    The budget is therefore shared out per item instead. Each entry is either
    reproduced in full or cut to its share and marked with a trailing
    ellipsis, so the reviewer can always tell a shortened entry from a
    complete one, and no entry ever vanishes while another is still whole.
    """

    texts = [_semantic_text(value) for value in omitted]
    if not texts:
        return {
            "digest": "",
            "source_chars": 0,
            "prose_complete": True,
            "item_coverage_complete": True,
            "elided_item_count": 0,
            "min_item_chars": DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS,
        }
    # One newline separates consecutive entries; that overhead comes out of
    # the budget first, so the shares can never render past the cap (the one
    # remaining way to exceed it -- separators alone outgrowing the cap -- is
    # handled after the join below).
    separators = len(texts) - 1
    source_chars = sum(len(text) for text in texts) + separators
    budget = max(0, DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS - separators)
    shares = _fair_share_digest_budgets([len(text) for text in texts], budget)
    entries: list[str] = []
    elided = 0
    for text, share in zip(texts, shares):
        if len(text) <= share:
            entries.append(text)
            continue
        elided += 1
        # Spend one character of the share on the marker so a truncated entry
        # is never mistaken for a short-but-complete one.
        entries.append((text[: max(0, share - 1)] + "\u2026") if share > 0 else "")
    covered = all(
        len(text) <= share
        or share >= DISCOVERY_REVIEW_SEMANTIC_DIGEST_MIN_ITEM_CHARS
        for text, share in zip(texts, shares)
    )
    digest = "\n".join(entries)
    if len(digest) > DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS:
        # Only reachable at a cardinality where the separators alone exceed
        # the cap (more than DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS + 1
        # omitted items): the budget clamps to zero, every share is zero, and
        # the join is a wall of newlines that would itself breach the bound
        # this field exists to respect. Clamp it, and withdraw the coverage
        # claim -- the tail items are not in the rendered field at any width.
        digest = digest[:DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS]
        covered = False
    return {
        "digest": digest,
        "source_chars": source_chars,
        # Exactly the historical semantic_digest_complete rule: the
        # untruncated join of every omitted item's text fits the cap, i.e.
        # every item is reproduced verbatim. This is equivalent to
        # ``elided == 0`` for every cohort whose separators fit the cap, and
        # is correctly False for the clamped cohorts above, where they do not.
        "prose_complete": source_chars <= DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS,
        "item_coverage_complete": covered,
        "elided_item_count": elided,
        "min_item_chars": min(shares),
    }


def _bounded_collection_coverage(
    values: list[dict[str, Any]],
    included: list[dict[str, Any]],
    *,
    omitted: list[dict[str, Any]],
    selection: str,
) -> dict[str, Any]:
    """Keep semantic coverage for omitted bodies without treating it as absence."""

    if len(omitted) <= DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS:
        sampled_indices = list(range(len(omitted)))
    elif DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS == 1:
        sampled_indices = [len(omitted) - 1]
    else:
        last = len(omitted) - 1
        sampled_indices = [
            round(index * last / (DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS - 1))
            for index in range(DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS)
        ]
    sampled_omitted = [omitted[index] for index in sampled_indices]
    omitted_semantic_index = []
    for ordinal, value in zip(sampled_indices, sampled_omitted):
        provenance = {
            key: str(value[key])[:240]
            for key in (
                "source_task_id",
                "document_id",
                "task_id",
                "artifact_id",
                "source_kind",
                "task_type",
                "type",
                "status",
            )
            if value.get(key) is not None
        }
        omitted_semantic_index.append(
            {
                "ordinal": ordinal,
                "item_checksum": _checksum(value),
                "semantic_excerpt": _semantic_excerpt(value, max_chars=240),
                **provenance,
            }
        )
    digest = _fit_semantic_digest(omitted)
    return {
        "total_count": len(values),
        "included_count": len(included),
        "omitted_count": max(0, len(values) - len(included)),
        "selection": selection,
        "full_set_checksum": _checksum(values),
        "omitted_semantic_index": omitted_semantic_index,
        "semantic_index_total_count": len(omitted),
        "semantic_index_included_count": len(sampled_omitted),
        "semantic_index_selection": "evenly_spaced_stable_order",
        "full_semantic_digest": digest["digest"],
        "semantic_digest_item_count": len(omitted),
        "semantic_digest_source_chars": digest["source_chars"],
        # Full prose fidelity: every omitted item reproduced verbatim. Kept at
        # its historical meaning and still reported, but no longer what binding
        # authority hangs on -- see the item-coverage flag below.
        "semantic_digest_complete": digest["prose_complete"],
        # The authority-bearing flag: every omitted item is accounted for, in
        # full or at the readable per-item floor. False only when the cohort is
        # so numerous that the floor cannot be paid for every item.
        "semantic_digest_item_coverage_complete": digest["item_coverage_complete"],
        "semantic_digest_elided_item_count": digest["elided_item_count"],
        "semantic_digest_min_item_chars": digest["min_item_chars"],
        "semantic_digest_order": "stable_collection_order",
    }


async def _get_or_add_evidence(
    session,
    *,
    cycle: DiscoveryCycle,
    dedupe_key: str,
    semantic_role: str,
    source_kind: str,
    source_id: str,
    content_type: str,
    head_sha: str | None,
    snapshot: Mapping[str, Any],
) -> DiscoveryEvidenceItem:
    existing = (
        await session.execute(
            select(DiscoveryEvidenceItem).where(
                DiscoveryEvidenceItem.cycle_id == cycle.id,
                DiscoveryEvidenceItem.dedupe_key == dedupe_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    item = DiscoveryEvidenceItem(
        cycle_id=cycle.id,
        semantic_role=semantic_role,
        source_kind=source_kind,
        source_id=source_id,
        checksum=_checksum(snapshot),
        content_type=content_type,
        head_sha=head_sha,
        mechanism_fingerprint=cycle.mechanism_fingerprint,
        evidence_metadata={
            "packet_version": DISCOVERY_REVIEW_PACKET_VERSION,
            "snapshot": json.loads(_canonical_json(snapshot)),
        },
        dedupe_key=dedupe_key,
    )
    session.add(item)
    await session.flush()
    return item


def _evidence_projection(item: DiscoveryEvidenceItem) -> dict[str, Any]:
    metadata, truncated = _bounded_metadata(item.evidence_metadata or {})
    result = {
        "evidence_id": str(item.id),
        "semantic_role": item.semantic_role,
        "source_kind": item.source_kind,
        "checksum": item.checksum,
        "source_id": item.source_id,
        "source_uri": str(item.source_uri or "")[:DISCOVERY_REVIEW_URI_CHARS]
        or None,
        "content_type": item.content_type,
        "head_sha": item.head_sha,
        "mechanism_fingerprint": item.mechanism_fingerprint,
        "evidence_metadata": metadata,
    }
    if truncated:
        result["evidence_metadata_truncated"] = True
    return result


def _slice_included_from_start(
    values: list[dict[str, Any]], count: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select up to ``count`` items from the start; return (included, omitted)."""

    if count <= 0:
        return [], list(values)
    return values[:count], values[count:]


def _slice_included_from_end(
    values: list[dict[str, Any]], count: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select up to ``count`` items from the end; return (included, omitted).

    ``values[-0:]`` is the whole list, not empty, so ``count == 0`` is
    special-cased rather than left to the slice syntax.
    """

    if count <= 0:
        return [], list(values)
    if len(values) <= count:
        return list(values), []
    return values[-count:], values[:-count]


# name -> (slice function, selection-template). The template's ``{count}``
# is the per-collection detail count actually included at the chosen
# degradation level, so the rendered selection string always describes what
# is really in the receipt.
_COLLECTION_DETAIL_CONFIG: dict[str, tuple[Any, str]] = {
    "research_findings": (
        _slice_included_from_start,
        "first_{count}_in_stable_task_order",
    ),
    "knowledge_outcomes": (
        _slice_included_from_end,
        "latest_{count}_in_stable_document_order",
    ),
    "landed_outcomes": (
        _slice_included_from_end,
        "latest_{count}_in_stable_task_order",
    ),
    "experiment_outcomes": (
        _slice_included_from_end,
        "latest_{count}_in_stable_artifact_order",
    ),
    "verification_summaries": (
        _slice_included_from_end,
        "latest_{count}_in_stable_task_order",
    ),
}

_COLLECTION_BASE_DETAIL_COUNTS: dict[str, int] = {
    "research_findings": DISCOVERY_REVIEW_RESEARCH_DETAILS,
    "knowledge_outcomes": DISCOVERY_REVIEW_KNOWLEDGE_DETAILS,
    "landed_outcomes": DISCOVERY_REVIEW_LANDED_OUTCOMES,
    "experiment_outcomes": DISCOVERY_REVIEW_EXPERIMENT_DETAILS,
    "verification_summaries": DISCOVERY_REVIEW_VERIFICATION_DETAILS,
}


def _progressive_detail_levels(
    base_counts: Mapping[str, int],
) -> list[dict[str, int]]:
    """Halve every collection's detail count together each round (e.g.
    6 -> 3 -> 1 -> 0), stopping once every count has reached zero. The final
    entry is therefore always the all-zero-detail level, so a caller that
    walks this list and still can't fit the packet has confirmed that even
    the zero-detail form overflows.
    """

    levels: list[dict[str, int]] = []
    current = dict(base_counts)
    while any(count > 0 for count in current.values()):
        current = {name: count // 2 for name, count in current.items()}
        levels.append(current)
    return levels


def _evidence_coverage_for_level(
    collections: Mapping[str, list[dict[str, Any]]],
    detail_counts: Mapping[str, int],
    *,
    level: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Slice every collection to ``detail_counts`` and build its coverage.

    ``level`` 0 is the full-detail packet, so its selection strings match the
    historical hardcoded text exactly; ``level`` above 0 appends a
    ``reduced_to_fit_total_packet_budget`` suffix so the receipt records that
    it was degraded to fit the packet budget.
    """

    included: dict[str, list[dict[str, Any]]] = {}
    coverage: dict[str, dict[str, Any]] = {}
    for name, values in collections.items():
        slice_fn, template = _COLLECTION_DETAIL_CONFIG[name]
        count = detail_counts[name]
        included_values, omitted_values = slice_fn(values, count)
        selection = template.format(count=count)
        if level > 0:
            selection = (
                f"{selection}|reduced_to_fit_total_packet_budget(level={level})"
            )
        included[name] = included_values
        coverage[name] = _bounded_collection_coverage(
            values,
            included_values,
            omitted=omitted_values,
            selection=selection,
        )
    return included, coverage


def _assemble_receipt(
    *,
    source_parent: dict[str, Any],
    included: Mapping[str, list[dict[str, Any]]],
    stranded_deliveries: list[dict[str, Any]],
    evidence_coverage: Mapping[str, dict[str, Any]],
    execution: dict[str, Any],
    missing_reasons: list[str],
    compacted: bool,
    authority_blockers: Sequence[str] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Assemble one candidate receipt at a given degradation level.

    The authority invariant is derived exactly as at full detail:
    ``binding_decision_allowed`` is ``not binding_action_blockers``, and a
    blocker exists only when some collection's
    ``semantic_digest_item_coverage_complete`` is False -- that is, only when
    an omitted item is genuinely unaccounted for, never merely because the
    digest had to shorten an entry's prose. Gating on prose fidelity instead
    is what stranded five directions at ``ask_operator`` on 2026-08-23: one
    verbose finding revoked authority over a cohort whose evidence the engine
    already held, and a human was asked to paste it back by hand.

    ``authority_blockers`` carries the caller's decision-snapshot blockers
    (a truncated intent/bet authority). They are unioned in here rather than
    recomputed per level because they do not vary with degradation level,
    and they must reach the receipt so its stored authority invariant agrees
    with the manifest's.
    """

    incomplete_collections = [
        name
        for name, coverage in evidence_coverage.items()
        if not coverage["semantic_digest_item_coverage_complete"]
    ]
    binding_action_blockers = list(authority_blockers) + (
        [
            "complete authored semantic coverage exceeds the bounded decision "
            f"packet for: {', '.join(incomplete_collections)}"
        ]
        if incomplete_collections
        else []
    )
    receipt = {
        "source_parent": source_parent,
        "research_findings": included["research_findings"],
        "knowledge_outcomes": included["knowledge_outcomes"],
        "landed_outcomes": included["landed_outcomes"],
        "stranded_deliveries": stranded_deliveries[:DISCOVERY_REVIEW_LANDED_OUTCOMES],
        "experiment_outcomes": included["experiment_outcomes"],
        "verification_summaries": included["verification_summaries"],
        "evidence_coverage": dict(evidence_coverage),
        "execution": execution,
        "missing_required_evidence": missing_reasons,
        "binding_action_blockers": binding_action_blockers,
        "binding_decision_allowed": not binding_action_blockers,
    }
    if compacted:
        receipt["receipt_compacted_to_budget"] = True
        # Only surfaced once budget pressure actually reduced a collection
        # below its full authored count -- distinguishes "trimmed" from
        # "genuinely has nothing" (an empty, never-truncated category never
        # appears here) so the reviewer never mistakes truncation for
        # absence.
        evidence_truncated = [
            f"{name}:kept_{coverage['included_count']}_of_{coverage['total_count']}"
            for name, coverage in evidence_coverage.items()
            if coverage["total_count"] > 0
            and coverage["included_count"] < coverage["total_count"]
        ]
        if evidence_truncated:
            receipt["evidence_truncated"] = evidence_truncated
    return receipt, binding_action_blockers


def _truncate_prose(value: Any, *, max_chars: int) -> Any:
    """Recursively cap every string leaf of an already-built item.

    Used only for the small handful of floor-repair items re-injected by
    ``_repair_zeroed_categories`` below: a representative item's raw prose
    (a full research finding, an artifact's embedded logs, ...) can itself be
    large enough to blow the packet budget, so once the floor guarantees the
    item is present at all, its bulk is capped instead.
    """

    if isinstance(value, str):
        return value[:max_chars]
    if isinstance(value, Mapping):
        return {
            key: _truncate_prose(inner, max_chars=max_chars)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [_truncate_prose(inner, max_chars=max_chars) for inner in value]
    return value


def _repair_zeroed_categories(
    *,
    collections: Mapping[str, list[dict[str, Any]]],
    included: Mapping[str, list[dict[str, Any]]],
    evidence_coverage: Mapping[str, dict[str, Any]],
    source_parent: dict[str, Any],
    stranded_deliveries: list[dict[str, Any]],
    execution: dict[str, Any],
    missing_reasons: list[str],
    authority_blockers: Sequence[str] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Never let a category with real evidence render as fully absent.

    The progressive-level loop in ``build_discovery_review_packet`` reduces
    every collection's *detail count* together, one round at a time, to fit
    the fixed packet budget. Because the rounds are shared, a small, cheap
    category (e.g. 2 landed outcomes) can be forced all the way to zero
    purely because a different, bulkier category (e.g. experiment artifacts
    embedding large logs/URIs) needed that many rounds of halving to shrink
    -- even though the cheap category alone would never have needed to
    disappear. Zero items reads to the reviewer as "nothing happened",
    indistinguishable from a category that is genuinely empty, and is
    exactly the production defect this function closes: a decision packet
    that silently drops every landed outcome and every research finding even
    though the direction had landed work.

    Whatever level the caller landed on (whether it already fits or is the
    final, most-reduced level), re-inject a small, prose-truncated floor for
    every category that has real underlying data but landed at zero
    included items, then re-verify the packet still fits the budget. Try
    progressively shorter per-item prose caps until it does, or until even
    the shortest cap can't close the gap -- in which case the caller's own
    last-resort fallback (which strips coverage bulk, not item content) gets
    one more chance to make it fit.
    """

    zeroed = [
        name
        for name, coverage in evidence_coverage.items()
        if coverage["included_count"] == 0 and coverage["total_count"] > 0
    ]
    if not zeroed:
        return _assemble_receipt(
            source_parent=source_parent,
            included=dict(included),
            stranded_deliveries=stranded_deliveries,
            evidence_coverage=dict(evidence_coverage),
            execution=execution,
            missing_reasons=missing_reasons,
            authority_blockers=authority_blockers,
            compacted=True,
        )

    receipt: dict[str, Any] = {}
    binding_action_blockers: list[str] = []
    for max_chars in DISCOVERY_REVIEW_FLOOR_PROSE_CHAR_LEVELS:
        candidate_included = dict(included)
        candidate_coverage = dict(evidence_coverage)
        for name in zeroed:
            values = collections[name]
            slice_fn, template = _COLLECTION_DETAIL_CONFIG[name]
            floor_count = min(DISCOVERY_REVIEW_MIN_CATEGORY_ITEMS, len(values))
            floor_selected, floor_omitted = slice_fn(values, floor_count)
            candidate_included[name] = [
                _truncate_prose(item, max_chars=max_chars)
                for item in floor_selected
            ]
            selection = (
                f"{template.format(count=len(floor_selected))}"
                f"|floor_repaired_prose_truncated_to_{max_chars}_chars"
            )
            candidate_coverage[name] = _bounded_collection_coverage(
                values,
                floor_selected,
                omitted=floor_omitted,
                selection=selection,
            )
        receipt, binding_action_blockers = _assemble_receipt(
            source_parent=source_parent,
            included=candidate_included,
            stranded_deliveries=stranded_deliveries,
            evidence_coverage=candidate_coverage,
            execution=execution,
            missing_reasons=missing_reasons,
            authority_blockers=authority_blockers,
            compacted=True,
        )
        if _receipt_fits_evidence_metadata(receipt):
            return receipt, binding_action_blockers
    # Every prose-cap level was tried; return the most aggressively truncated
    # attempt so the caller's coverage-bulk fallback (which never touches
    # item content) gets a chance to close the remaining gap without
    # re-emptying the categories this function just floor-repaired.
    return receipt, binding_action_blockers


async def build_discovery_review_packet(
    session,
    cycle: DiscoveryCycle,
    *,
    now: datetime | None = None,
) -> tuple[
    list[dict[str, Any]],
    str,
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Build one bounded semantic receipt for the settled research parent."""

    now = now or _utcnow()
    bounded_intent, intent_truncated, intent_authority_lost = (
        _bounded_decision_snapshot(
            cycle.intent_snapshot,
            authority_keys=DISCOVERY_REVIEW_INTENT_AUTHORITY_KEYS,
        )
    )
    bounded_bet, bet_truncated, bet_authority_lost = _bounded_decision_snapshot(
        cycle.bet_snapshot,
        authority_keys=DISCOVERY_REVIEW_BET_AUTHORITY_KEYS,
    )
    tasks = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == cycle.project_id,
                    Task.discovery_cycle_id == cycle.id,
                    Task.id != cycle.review_task_id
                    if cycle.review_task_id is not None
                    else True,
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    source = next(
        (task for task in tasks if task.id == cycle.source_research_task_id), None
    )
    task_ids = [task.id for task in tasks]
    artifacts = (
        list(
            (
                await session.execute(
                    select(Artifact)
                    .where(Artifact.task_id.in_(task_ids))
                    .order_by(Artifact.created_at.asc(), Artifact.id.asc())
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        if task_ids
        else []
    )
    artifacts_by_task: dict[uuid.UUID, list[Artifact]] = {}
    for artifact in artifacts:
        artifacts_by_task.setdefault(artifact.task_id, []).append(artifact)
    knowledge_documents = (
        list(
            (
                await session.execute(
                    select(KnowledgeDocument)
                    .where(KnowledgeDocument.source_task_id.in_(task_ids))
                    .order_by(KnowledgeDocument.created_at.asc(), KnowledgeDocument.id.asc())
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        if task_ids
        else []
    )

    landed: list[dict[str, Any]] = []
    stranded_deliveries: list[dict[str, Any]] = []
    missing: list[str] = []
    # A truncated intent/bet snapshot is bounded, not absent: the packet still
    # carries every top-level key of the authority, shortened where it had to
    # be, plus the full authority's checksum, and the manifest flags the
    # truncation outright. It is also unresolvable by any amount of waiting --
    # the "missing" evidence is the direction's own length, which no task,
    # retry, or agent can shrink -- so recording it as
    # missing_required_evidence held the cycle in EVIDENCE_PENDING forever and
    # silently starved the direction (no review spawned, no operator asked).
    # Treat it the way the packet already treats coverage that cannot fit the
    # bounded budget: forbid binding action and still dispatch one
    # operator-question decision. required_evidence_omitted stays True either
    # way, so the "only ask_operator is permitted" invariant is unchanged.
    #
    # What changed on 2026-08-23: the blocker no longer hangs on *whether*
    # something was shortened, only on whether a decision-bearing key could
    # not be carried whole. Gating on the mere fact of truncation is what
    # stranded d-f502e1a5 at ask_operator across two generations from
    # 2026-08-19: the field that overflowed was the accumulated ``direction``
    # envelope, whose text the packet already hands the reviewer through
    # ``claim`` and ``source_parent.title``, and the engine asked a human for
    # authority it was holding intact. The named keys are appended to the
    # blocker so an operator reading the manifest can see which part of the
    # authority was actually lost, instead of only that some part was.
    authority_blockers: list[str] = []
    if intent_authority_lost:
        authority_blockers.append(
            "intent authority exceeds the bounded decision contract: "
            f"{intent_authority_lost}"
        )
    if bet_authority_lost:
        authority_blockers.append(
            "bet authority exceeds the bounded decision contract: "
            f"{bet_authority_lost}"
        )
    if source is None:
        missing.append("source research parent is missing from the discovery cohort")
    # A research parent that settled FAILED/CANCELLED never rolls its staging
    # branch up, so a child PR merged into that branch can never gain a landed
    # receipt. That absence is a settled fact for the review to judge (salvage,
    # re-bet, or stop) — not evidence still in flight — so holding the cycle in
    # EVIDENCE_PENDING on it would starve the direction forever.
    source_settled_unlanded = source is not None and source.status in {
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    for task in tasks:
        task_type = _task_type(task)
        is_source_parent = task.id == cycle.source_research_task_id
        if (
            task_type not in WRITE_TASK_TYPES
            and not is_source_parent
        ) or task.status != TaskStatus.COMPLETED:
            continue
        task_artifacts = artifacts_by_task.get(task.id, [])
        commit_hash = _merge_sha(task)
        pr_number = _pr_number(task)
        landed_signal = has_landed_signal(task)
        if pr_number is not None and not landed_signal:
            if source_settled_unlanded:
                stranded_deliveries.append(
                    {
                        "task_id": str(task.id),
                        "task_type": task_type,
                        "pr_number": pr_number,
                        "reason": (
                            "research parent settled "
                            f"{_enum_value(source.status)} before rollup"
                        ),
                    }
                )
            else:
                missing.append(
                    f"delivery task {task.id} has PR {pr_number} without landed receipt"
                )
        if landed_signal and not commit_hash:
            missing.append(f"landed write task {task.id} lacks exact commit identity")
        if landed_signal:
            landed.append(
                {
                    "task_id": str(task.id),
                    "task_type": task_type,
                    "pr_number": pr_number,
                    "merge_commit_sha": commit_hash,
                    "artifacts": [
                        _compact_artifact(artifact) for artifact in task_artifacts
                    ],
                }
            )

    experiments = [
        _compact_artifact(artifact)
        for artifact in artifacts
        if _artifact_type(artifact) in _EXPERIMENT_ARTIFACT_TYPES
    ]
    missing.extend(
        f"compute experiment {task.id} lacks COMPUTE_RESULT receipt"
        for task in tasks
        if (
            _task_type(task) == "compute_experiment"
            and task.status == TaskStatus.COMPLETED
            and not any(
                artifact.task_id == task.id
                for artifact in artifacts
                if _artifact_type(artifact) == ArtifactType.COMPUTE_RESULT.value
            )
        )
    )

    timestamps = [task.created_at for task in tasks if task.created_at is not None]
    endpoints = [
        task.completed_at or task.updated_at
        for task in tasks
        if (task.completed_at or task.updated_at) is not None
    ]
    elapsed_seconds = None
    if timestamps and endpoints:
        elapsed_seconds = max(0.0, (max(endpoints) - min(timestamps)).total_seconds())
    total_cost = sum(float(task.estimated_cost_usd or 0.0) for task in tasks)
    research_findings = [
        {
            "source_task_id": str(task.id),
            "task_type": _task_type(task),
            "finding": _bounded_json_value(finding, max_chars=700),
            "semantic_summary": _canonical_semantic_value(finding),
            "finding_checksum": _checksum(finding),
        }
        for task in tasks
        for finding in finalized_research_findings(task)
    ]
    knowledge_outcomes = [
        {
            "document_id": str(document.id),
            "source_task_id": str(document.source_task_id)
            if document.source_task_id
            else None,
            "source_kind": str(document.source_kind),
            "title": str(document.title or "")[:240],
            "summary": str(document.summary or "")[:500],
            "topic": str(document.topic or "")[:240] or None,
            "citations": _bounded_json_value(
                list(document.source_citations or [])[:12], max_chars=500
            ),
            "semantic_summary": _canonical_semantic_value(
                {
                    "title": document.title,
                    "summary": document.summary,
                    "topic": document.topic,
                    "source_citations": document.source_citations,
                }
            ),
        }
        for document in knowledge_documents
    ]
    verification_summaries = [
        _verification_summary(task)
        for task in tasks
        if _task_type(task) in _VERIFICATION_TASK_TYPES
    ]
    source_parent = {
        "task_id": str(source.id) if source else None,
        "title": str(source.title or "")[:500] if source else None,
        "status": str(_enum_value(source.status)) if source else None,
        "latest_status": str(source.latest_status_message or "")[:1_000]
        if source
        else None,
    }
    execution = {
        "task_count": len(tasks),
        "retry_count": sum(int(task.attempt_count or 0) for task in tasks),
        "failed_count": sum(task.status == TaskStatus.FAILED for task in tasks),
        "cancelled_count": sum(task.status == TaskStatus.CANCELLED for task in tasks),
        "elapsed_seconds": round(elapsed_seconds, 3)
        if elapsed_seconds is not None
        else None,
        "estimated_cost_usd": round(total_cost, 6),
    }
    missing_reasons = _bounded_missing_reasons(missing)
    collections: dict[str, list[dict[str, Any]]] = {
        "research_findings": research_findings,
        "knowledge_outcomes": knowledge_outcomes,
        "landed_outcomes": landed,
        "experiment_outcomes": experiments,
        "verification_summaries": verification_summaries,
    }
    included, evidence_coverage = _evidence_coverage_for_level(
        collections, _COLLECTION_BASE_DETAIL_COUNTS, level=0
    )
    receipt, binding_action_blockers = _assemble_receipt(
        source_parent=source_parent,
        included=included,
        stranded_deliveries=stranded_deliveries,
        evidence_coverage=evidence_coverage,
        execution=execution,
        missing_reasons=missing_reasons,
        authority_blockers=authority_blockers,
        compacted=False,
    )
    if not _receipt_fits_evidence_metadata(receipt):
        # Detail-rich receipts may exceed the fixed controller budget even when
        # every outcome exists. Progressively halve every collection's
        # included-detail count each round (e.g. 6 -> 3 -> 1 -> 0), rebuilding
        # the receipt at each level, and stop at the first size that fits.
        # This keeps as many collections' authored details visible as the
        # packet budget allows instead of zeroing every collection at once
        # the moment the full-detail form overflows. Coverage counts,
        # checksums, sampled provenance, and per-item semantic coverage stay
        # intact at every level, so binding_decision_allowed keeps depending
        # solely on whether every omitted item is accounted for, never on
        # which detail level was chosen. Reducing the level moves items from
        # included to omitted and so shortens their digest entries; that
        # costs prose fidelity, which is reported, but not authority.
        for level, detail_counts in enumerate(
            _progressive_detail_levels(_COLLECTION_BASE_DETAIL_COUNTS), start=1
        ):
            included, evidence_coverage = _evidence_coverage_for_level(
                collections, detail_counts, level=level
            )
            receipt, binding_action_blockers = _assemble_receipt(
                source_parent=source_parent,
                included=included,
                stranded_deliveries=stranded_deliveries,
                evidence_coverage=evidence_coverage,
                execution=execution,
                missing_reasons=missing_reasons,
                authority_blockers=authority_blockers,
                compacted=True,
            )
            if _receipt_fits_evidence_metadata(receipt):
                break
        # Whichever level the loop landed on -- a level that fit, or the
        # final, most-reduced level if none did -- never let it render a
        # category that truly has evidence as fully empty. Re-inject a
        # small, prose-truncated floor for any collection the lockstep
        # halving above zeroed out even though it had real items.
        receipt, binding_action_blockers = _repair_zeroed_categories(
            collections=collections,
            included=included,
            evidence_coverage=evidence_coverage,
            source_parent=source_parent,
            stranded_deliveries=stranded_deliveries,
            execution=execution,
            missing_reasons=missing_reasons,
            authority_blockers=authority_blockers,
        )
        # _assemble_receipt always mirrors its evidence_coverage argument
        # into receipt["evidence_coverage"], so read the repaired coverage
        # back from the receipt rather than the pre-repair local variable --
        # the pathological fallback below mutates whichever one it sees.
        evidence_coverage = receipt["evidence_coverage"]
    if not _receipt_fits_evidence_metadata(receipt):
        # Pathological cardinality can also overflow the compact semantic
        # indexes even after the floor repair above. Preserve exact
        # counts/checksums, forbid binding action, and still dispatch one
        # operator-question decision instead of stranding the completed
        # cohort behind an impossible evidence wait. This does not touch the
        # floor-repaired item lists themselves, only the omitted-item
        # coverage bulk, so a category the floor repair just restored stays
        # restored.
        for coverage in evidence_coverage.values():
            coverage["omitted_semantic_index"] = []
            coverage["semantic_index_included_count"] = 0
            coverage["full_semantic_digest"] = ""
            coverage["semantic_digest_complete"] = False
            coverage["semantic_digest_item_coverage_complete"] = False
            coverage["semantic_digest_elided_item_count"] = coverage["omitted_count"]
            coverage["semantic_digest_min_item_chars"] = 0
        binding_action_blockers = list(authority_blockers) + [
            "complete authored semantic coverage cannot fit the bounded decision packet"
        ]
        receipt.update(
            {
                "evidence_coverage": evidence_coverage,
                "binding_action_blockers": binding_action_blockers,
                "binding_decision_allowed": False,
                "receipt_compacted_to_budget": True,
            }
        )
    landed_sha = next(
        (
            str(item.get("merge_commit_sha"))
            for item in reversed(landed)
            if item.get("merge_commit_sha")
        ),
        None,
    )
    outcome_item = await _get_or_add_evidence(
        session,
        cycle=cycle,
        dedupe_key=(
            f"parent-outcome:v{DISCOVERY_REVIEW_PACKET_VERSION}:{_checksum(receipt)}"
        ),
        semantic_role="parent_outcome",
        source_kind="research_parent",
        source_id=str(cycle.source_research_task_id),
        content_type="application/vnd.blueprint.discovery-outcome+json",
        head_sha=landed_sha,
        snapshot=receipt,
    )
    manifest = {
        "version": DISCOVERY_REVIEW_PACKET_VERSION,
        "source_research_task_id": str(cycle.source_research_task_id),
        "source_research_findings_finalized": bool(
            source is not None
            and source.status == TaskStatus.COMPLETED
            and isinstance(source.context, Mapping)
            and source.context.get(FINALIZED_RESEARCH_FINDINGS_KEY) is True
        ),
        "task_count": len(tasks),
        "artifact_count": len(artifacts),
        "semantic_receipt_checksum": outcome_item.checksum,
        "exact_landed_sha": landed_sha,
        "stranded_delivery_count": len(stranded_deliveries),
        "missing_required_evidence": missing_reasons,
        "binding_action_blockers": binding_action_blockers,
        "binding_decision_allowed": not binding_action_blockers,
        "required_evidence_omitted": bool(missing_reasons or binding_action_blockers),
        "intent_snapshot_truncated": intent_truncated,
        "bet_snapshot_truncated": bet_truncated,
    }
    manifest_item = await _get_or_add_evidence(
        session,
        cycle=cycle,
        dedupe_key=(
            f"packet-manifest:v{DISCOVERY_REVIEW_PACKET_VERSION}:"
            f"{_checksum(manifest)}"
        ),
        semantic_role="packet_manifest",
        source_kind="server_ledger",
        source_id=str(cycle.id),
        content_type="application/vnd.blueprint.discovery-review-manifest+json",
        head_sha=landed_sha,
        snapshot=manifest,
    )
    packet = [
        _evidence_projection(item)
        for item in [manifest_item, outcome_item]
    ]
    artifact_prerequisites = [
        {
            "artifact_id": str(artifact.id),
            "artifact_type": _artifact_type(artifact),
            "source_task_id": str(artifact.task_id),
            "storage_uri": str(artifact.storage_uri)[:DISCOVERY_REVIEW_URI_CHARS],
        }
        for artifact in artifacts
        if artifact.storage_uri
    ][:12]
    return (
        packet,
        _checksum(packet),
        artifact_prerequisites,
        manifest,
        bounded_intent,
        bounded_bet,
    )
