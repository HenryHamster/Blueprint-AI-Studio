"""First-class BLOCKED task state — durable blocker metadata + transitions.

A task is BLOCKED when the work is wanted but not currently runnable because a
precondition is absent: an upstream dependency/rollup hasn't landed, a base
branch or referenced file doesn't exist yet, or the agent surfaced a
``BLOCKER:`` message. BLOCKED is distinct from FAILED — it does not count as a
code/infra failure, does not burn the retry budget, and is recoverable. The
in-transition (FAILED/agent-blocker -> BLOCKED) lives here alongside the
out-transition (BLOCKED -> QUEUED) so the pair is defined in one place; the
recurring recheck/escalation sweep that decides *when* to call the
out-transition is owned by the auto-unblock work.

Durable blocker metadata is stored under ``Task.context['blocker']`` so the
state is additive and backward-compatible (no migration required). The accessor
surface here is intentionally column-shaped (``waiting_on``, ``reason``,
``recheck_at``, ``deadline_at``, ``escalated``) so a later migration can promote
these keys to dedicated columns without changing call sites.

Backward compatibility: ``enter_blocked_state`` also writes the canonical
``BLOCKED_PRECONDITION`` failure class into ``context['failure_category']`` /
``context['failure_class']`` so existing consumers (digest metrics, analytics,
failure-taxonomy) keep classifying blocked work exactly as before, and so
historical rows that terminated FAILED with that class remain comparable.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm.attributes import flag_modified

from app.models import (
    Task,
    TaskStatus,
    advance_task_lifetime_attempt,
    bank_task_current_attempt,
)
from app.research_execution_authority import RESEARCH_FRESH_ATTEMPT_CONTEXT_KEYS
from app.sidecar_authority import (
    SIDECAR_FRESH_ATTEMPT_CONTEXT_KEYS,
    authenticated_sidecar_fresh_attempt_preserve_keys,
)
from app.time_utils import _utcnow, clear_retry_after

# Canonical failure class for precondition blockers (mirrors
# app.failure_classification.BLOCKED_PRECONDITION; duplicated as a literal here
# to avoid importing the heavy classification module into this leaf helper).
BLOCKED_PRECONDITION = "BLOCKED_PRECONDITION"

# JSONB key under Task.context that holds the structured blocker metadata.
BLOCKER_CONTEXT_KEY = "blocker"
PUBLICATION_GATE_RECEIPT_KEY = "publication_gate_receipt"
PUBLICATION_GATE_RECEIPT_HISTORY_KEY = "publication_gate_receipt_history"
_PUBLICATION_GATE_RECEIPT_HISTORY_LIMIT = 8
PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES = 100
PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH = 512
PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH = 4000
# Attribution is durable JSONB that is copied into the bounded receipt history.
# Bound the serialized attribution block itself, rather than multiplying field
# caps: four buckets at their individual maxima would otherwise make one
# heartbeat carry megabytes and eight archived receipts amplify that cost.
PUBLICATION_GATE_ATTRIBUTION_MAX_BYTES = 32 * 1024
_PUBLICATION_GATE_ATTRIBUTION_BUCKET_KEYS = (
    "new_failures",
    "suppressed_failures",
    "disappeared_failures",
    "skipped_failures",
)
_PUBLICATION_GATE_ATTRIBUTION_DETAIL_KEYS = ("detail", "reason")

# Cross-lane guard-suppression projection ("guard_suppressions"): every guard
# lane's suppressions (static, vitest, typecheck, viewport, backend pytest —
# see scripts/guard_suppression_receipt.py), not just the backend pytest
# lane's. Bounded independently of the attribution buckets above so one
# oversized aggregate cannot crowd out the pytest attribution already in the
# same receipt.
GUARD_SUPPRESSIONS_MAX_LANES = 12
GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE = 25
GUARD_SUPPRESSIONS_STATE_MAX_LENGTH = 50
GUARD_SUPPRESSIONS_MAX_BYTES = 16 * 1024


def _sanitize_publication_gate_attribution_bucket(
    raw: object,
) -> tuple[dict[str, str | dict[str, str]], int] | None:
    if not isinstance(raw, dict):
        return None
    sanitized: dict[str, str | dict[str, str]] = {}
    for identity, value in sorted(raw.items(), key=lambda item: str(item[0])):
        if not isinstance(identity, str):
            return None
        bounded_identity = identity[:PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH]
        if bounded_identity in sanitized:
            # The collision is introduced by our identity bound. Keep the
            # deterministic first survivor and count the dropped identity.
            continue
        if isinstance(value, str):
            sanitized[bounded_identity] = value[:PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH]
        elif isinstance(value, dict):
            bounded_value: dict[str, str] = {}
            for key, member in value.items():
                if not isinstance(key, str):
                    return None
                if key not in _PUBLICATION_GATE_ATTRIBUTION_DETAIL_KEYS:
                    continue
                if not isinstance(member, str):
                    return None
                bounded_value[key] = member[:PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH]
            if "detail" not in bounded_value:
                return None
            sanitized[bounded_identity] = bounded_value
        else:
            return None
        if len(sanitized) >= PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES:
            break
    return sanitized, len(raw)


def _publication_gate_attribution_bytes(block: dict[str, object]) -> int:
    return len(json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _bound_publication_gate_attribution(
    attribution: dict[str, object],
    observed_counts: dict[str, int],
) -> dict[str, object]:
    """Keep the complete attribution projection within its JSONB budget.

    Once the per-bucket caps and counts are applied, discard the lexicographically
    greatest identity from the lowest-priority bucket first.  The reverse bucket
    order is fixed here so repeated heartbeats produce the same durable receipt.
    """
    counts: dict[str, dict[str, int]] = {}
    for bucket_key, observed_count in observed_counts.items():
        bucket = attribution.get(bucket_key)
        if isinstance(bucket, dict):
            counts[bucket_key] = {"observed": observed_count, "kept": len(bucket)}
    attribution["attribution_counts"] = counts
    while _publication_gate_attribution_bytes(attribution) > PUBLICATION_GATE_ATTRIBUTION_MAX_BYTES:
        eviction_bucket_key = next(
            (key for key in reversed(_PUBLICATION_GATE_ATTRIBUTION_BUCKET_KEYS) if attribution.get(key)),
            None,
        )
        if eviction_bucket_key is None:
            break
        bucket = attribution[eviction_bucket_key]
        if not isinstance(bucket, dict):
            break
        bucket.pop(next(reversed(bucket)))
        counts[eviction_bucket_key]["kept"] = len(bucket)
    return attribution


_GUARD_SUPPRESSIONS_BUCKETS = ("suppressed", "disappeared", "skipped")


def _sanitize_guard_suppressions(
    raw: object,
) -> tuple[dict[str, object], dict[str, int]] | None:
    """Bound the cross-lane guard-suppression projection.

    ``raw`` is ``{lane: {"state": str, "<bucket>": {identity: detail},
    "<bucket>_observed_count": int, "<bucket>_truncated": bool}}`` for each of
    the suppressed/disappeared/skipped buckets, the shape
    ``guard_suppression_receipt.project_for_gate_receipt`` derives from the
    aggregate. Two invariants are load-bearing:

    * All three buckets are kept. Some lanes record their only identity in
      ``disappeared`` or ``skipped`` (the viewport lane's capture-skipped
      branch), so a suppressed-only projection would drop the content the
      receipt exists to make reviewable.
    * The producer's ``observed_count`` wins over ``len()`` of the projection.
      The producer already caps each bucket at its own limit, so deriving the
      count here could never reveal that truncation happened.

    Only ``suppressed`` is required; ``disappeared``/``skipped`` are optional so
    an older agent's projection still persists. A genuinely malformed shape
    (wrong types) rejects the whole receipt, matching the existing
    attribution-bucket contract; a well-formed but over-cap payload is truncated
    deterministically instead so a valid gate verdict is never lost.
    """
    if not isinstance(raw, dict):
        return None
    observed_lanes = len(raw)
    sanitized: dict[str, object] = {}
    for lane, record in sorted(raw.items(), key=lambda item: str(item[0])):
        if not isinstance(lane, str) or not isinstance(record, dict):
            return None
        state = record.get("state")
        if not isinstance(state, str) or not isinstance(record.get("suppressed"), dict):
            return None
        lane_record: dict[str, object] = {"state": state[:GUARD_SUPPRESSIONS_STATE_MAX_LENGTH]}
        for bucket in _GUARD_SUPPRESSIONS_BUCKETS:
            entries = record.get(bucket)
            if entries is None and bucket != "suppressed":
                continue
            if not isinstance(entries, dict):
                return None
            bounded_identities: dict[str, str] = {}
            for identity, detail in sorted(entries.items(), key=lambda item: str(item[0])):
                if not isinstance(identity, str) or not isinstance(detail, str):
                    return None
                bounded_identity = identity[:PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH]
                if bounded_identity in bounded_identities:
                    continue
                bounded_identities[bounded_identity] = detail[
                    :PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH
                ]
                if len(bounded_identities) >= GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE:
                    break
            producer_observed = record.get(f"{bucket}_observed_count")
            observed_identities = (
                producer_observed
                if isinstance(producer_observed, int)
                and not isinstance(producer_observed, bool)
                and producer_observed >= len(bounded_identities)
                else len(entries)
            )
            lane_record[bucket] = bounded_identities
            lane_record[f"{bucket}_observed_count"] = observed_identities
            lane_record[f"{bucket}_truncated"] = bool(record.get(f"{bucket}_truncated")) or (
                observed_identities > len(bounded_identities)
            )
        bounded_lane = lane[:PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH]
        if bounded_lane in sanitized:
            continue
        sanitized[bounded_lane] = lane_record
        if len(sanitized) >= GUARD_SUPPRESSIONS_MAX_LANES:
            break
    return sanitized, {"observed_lanes": observed_lanes, "kept_lanes": len(sanitized)}


def _guard_suppressions_bytes(block: dict[str, object]) -> int:
    return len(json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _bound_guard_suppressions(
    sanitized: dict[str, object], counts: dict[str, int]
) -> dict[str, object]:
    """Evict entries, lexicographically-last lane first, until within budget.

    Eviction walks the buckets lowest-audit-value first (skipped, then
    disappeared, then suppressed) and leaves ``<bucket>_observed_count``
    untouched while flipping ``<bucket>_truncated`` on, so a reader can still
    tell the persisted set is partial.
    """
    block = {"guard_suppressions": sanitized, "guard_suppressions_counts": counts}
    while _guard_suppressions_bytes(block) > GUARD_SUPPRESSIONS_MAX_BYTES and sanitized:
        last_lane_key = next(reversed(sanitized))
        lane_record = sanitized[last_lane_key]
        evicted = False
        if isinstance(lane_record, dict):
            for bucket in reversed(_GUARD_SUPPRESSIONS_BUCKETS):
                entries = lane_record.get(bucket)
                if isinstance(entries, dict) and entries:
                    entries.pop(next(reversed(entries)))
                    lane_record[f"{bucket}_truncated"] = True
                    evicted = True
                    break
        if evicted:
            continue
        sanitized.pop(last_lane_key)
    counts["kept_lanes"] = len(sanitized)
    return block


def sanitize_publication_gate_receipt(raw: object) -> dict[str, object] | None:
    """Return a bounded local-publication-gate receipt or ``None``.

    Gate receipts arrive from an agent-owned shell script, so the JSONB boundary
    must not persist arbitrary keys or unbounded strings.  Requiring the full
    receipt shape also prevents a partial/malformed report from looking like a
    durable gate verdict.
    """
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    command = raw.get("command")
    recorded_at = raw.get("recorded_at")
    if not all(isinstance(value, str) for value in (status, command, recorded_at)):
        return None
    integer_fields = {
        "timeout_seconds": (raw.get("timeout_seconds"), 1, 900),
        "hard_timeout_seconds": (raw.get("hard_timeout_seconds"), 1, 1800),
        "elapsed_seconds": (raw.get("elapsed_seconds"), 0, 86400),
        "exit_code": (raw.get("exit_code"), 0, 255),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper
        for value, lower, upper in integer_fields.values()
    ):
        return None
    if not isinstance(raw.get("deadline_extended"), bool) or not isinstance(
        raw.get("authoritative_ci_required"), bool
    ):
        return None
    if not status or len(status) > 50 or not command or len(command) > 200:
        return None
    if not recorded_at or len(recorded_at) > 64:
        return None
    receipt: dict[str, object] = {
        "status": status,
        "command": command,
        "timeout_seconds": integer_fields["timeout_seconds"][0],
        "hard_timeout_seconds": integer_fields["hard_timeout_seconds"][0],
        "elapsed_seconds": integer_fields["elapsed_seconds"][0],
        "deadline_extended": raw["deadline_extended"],
        "exit_code": integer_fields["exit_code"][0],
        "authoritative_ci_required": raw["authoritative_ci_required"],
        "recorded_at": recorded_at,
    }
    if "attribution_state" in raw and raw["attribution_state"] is not None:
        attribution_state = raw["attribution_state"]
        if not isinstance(attribution_state, str):
            return None
        receipt["attribution_state"] = attribution_state[
            :PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH
        ]
    observed_counts: dict[str, int] = {}
    attribution: dict[str, object] = {}
    if "attribution_state" in receipt:
        attribution["attribution_state"] = receipt["attribution_state"]
    for bucket_key in _PUBLICATION_GATE_ATTRIBUTION_BUCKET_KEYS:
        if bucket_key in raw and raw[bucket_key] is not None:
            bounded_bucket = _sanitize_publication_gate_attribution_bucket(raw[bucket_key])
            if bounded_bucket is None:
                return None
            bucket, observed_count = bounded_bucket
            attribution[bucket_key] = bucket
            observed_counts[bucket_key] = observed_count
    if attribution:
        receipt.update(_bound_publication_gate_attribution(attribution, observed_counts))
    if "guard_suppressions" in raw and raw["guard_suppressions"] is not None:
        bounded_guard_suppressions = _sanitize_guard_suppressions(raw["guard_suppressions"])
        if bounded_guard_suppressions is None:
            return None
        sanitized_lanes, lane_counts = bounded_guard_suppressions
        receipt.update(_bound_guard_suppressions(sanitized_lanes, lane_counts))
    return receipt


def record_publication_gate_receipt(task: Task, raw: object) -> bool:
    """Make the latest gate verdict authoritative without losing prior runs.

    ``./pr open`` can run the publication gate repeatedly in one execution
    episode.  The old writer replaced the current receipt in-place, which made
    a later pass truthful but erased the failure that caused the repair loop.
    Keep the existing current-projection key for every reader, while moving a
    distinct prior projection into a small bounded history before replacement.

    Returns ``True`` iff the task context changed.  Malformed receipts remain a
    no-op at this boundary, matching :func:`sanitize_publication_gate_receipt`.
    """

    receipt = sanitize_publication_gate_receipt(raw)
    if receipt is None:
        return False
    ctx = dict(task.context or {})
    prior = ctx.get(PUBLICATION_GATE_RECEIPT_KEY)
    if prior == receipt:
        return False
    if isinstance(prior, dict):
        raw_history = ctx.get(PUBLICATION_GATE_RECEIPT_HISTORY_KEY)
        history = list(raw_history) if isinstance(raw_history, list) else []
        history.append(prior)
        ctx[PUBLICATION_GATE_RECEIPT_HISTORY_KEY] = history[
            -_PUBLICATION_GATE_RECEIPT_HISTORY_LIMIT:
        ]
    ctx[PUBLICATION_GATE_RECEIPT_KEY] = receipt
    task.context = ctx
    return True


def supersede_failed_publication_gate_receipt_on_landing(task: Task) -> bool:
    """Remove a stale failed *current* verdict after definitive integration.

    Older agents only reported failing publication gates.  Once GitHub proves
    that the exact task's PR landed, leaving that failure in the current slot is
    contradictory.  Preserve it in bounded history with an explicit
    supersession marker, then clear only the current projection.  Passing gate
    receipts remain current because they already agree with the landing.
    """

    ctx = dict(task.context or {})
    prior = ctx.get(PUBLICATION_GATE_RECEIPT_KEY)
    if not isinstance(prior, dict) or str(prior.get("status") or "").startswith(
        "passed"
    ):
        return False
    return _archive_current_publication_gate_receipt(
        task, superseded_by="integration_landing"
    )


def _archive_current_publication_gate_receipt(
    task: Task, *, superseded_by: str
) -> bool:
    """Archive and clear the current gate projection at an authority boundary."""

    ctx = dict(task.context or {})
    if PUBLICATION_GATE_RECEIPT_KEY not in ctx:
        return False
    prior = ctx.pop(PUBLICATION_GATE_RECEIPT_KEY)
    raw_history = ctx.get(PUBLICATION_GATE_RECEIPT_HISTORY_KEY)
    history = list(raw_history) if isinstance(raw_history, list) else []
    if isinstance(prior, dict):
        history.append({**prior, "superseded_by": superseded_by})
        ctx[PUBLICATION_GATE_RECEIPT_HISTORY_KEY] = history[
            -_PUBLICATION_GATE_RECEIPT_HISTORY_LIMIT:
        ]
    task.context = ctx
    return True

# Attempt-scoped context keys that must not survive any path returning a task
# (or clone) to QUEUED. Retry-guidance history such as
# ``_previous_failure_class`` / ``_failure_class_history`` is intentionally not
# listed here: it describes prior attempts and is consumed by retry prompts.
FRESH_ATTEMPT_CONTEXT_STRIP_KEYS: frozenset[str] = frozenset(
    {
        "failure_class",
        "failure_category",
        BLOCKER_CONTEXT_KEY,
        # Current gate verdicts are execution-episode projections. The reset
        # primitive archives them before stripping so history survives while a
        # fresh attempt truthfully starts with no current verdict.
        PUBLICATION_GATE_RECEIPT_KEY,
        "_blocked_escalated",
        "_precondition_escalated_key",
        "_blocked_optimistic_retries",
        "_blocked_last_retry_at",
        "_blocked_retry_precondition_receipt",
        "_retry_after_resets",
        "provision_failures",
        "provision_failures_infra",
        "retry_after",
        "max_plan_account_id",
        "llm_account_id",
        "llm_provider",
        "llm_auth_mode",
        "llm_model",
        "llm_execution_mode",
        # The concrete runner selected for the just-finished attempt is runtime
        # identity too. Keep operator intent (forced/preferred runner keys),
        # but never pin a fresh attempt back to the prior concrete runner.
        "runner_target_id",
        "runner_hostname",
        "agent_error_report",
        "external_api_subtype",
        "provider_failure_inferred_from",
        "dead_auth_matched_pattern",
        "rate_limit_kind",
        "rate_limit_retry_after_seconds",
        "rate_limit_matched_pattern",
        "rate_limit_correlation_reclassified",
        "rate_limit_inferred_from",
        "_requested_payload_model",
        "_resolved_payload_model",
        *RESEARCH_FRESH_ATTEMPT_CONTEXT_KEYS,
        # A fresh source artifact must negotiate and receive fresh sidecar
        # verdicts. Retaining a prior pass bypasses grading; retaining an
        # awaiting marker strands the new attempt. Source-link keys are absent
        # from this set so a requeued grader can still route its eventual result.
        *SIDECAR_FRESH_ATTEMPT_CONTEXT_KEYS,
        "_infra_wait",
        "blacklisted_accounts",
        "provision_failed_accounts",
        "_slot_released",
        "_api_failure_retries",
        "_dispatch_prep_failures",
        # Transient lane/warm routing is per-attempt and must be reattached by
        # an explicit spawn path if still relevant.
        "improvement_lane_id",
        "is_lane_phase",
        "lane_checkpoint_sha",
        "lane_resume",
        "warm_target_vm_id",
        "warm_target_pr",
        "warm_handoff_deadline",
        # Recovery budgets and terminalization evidence belong to the prior
        # attempt. A manual/automatic fresh requeue must start at attempt zero.
        "_childless_post_commit_recovery",
        "_childless_post_commit_abandoned",
    }
)

# ── Canonical machine-resolvable NEED primitive ──────────────────────────────
# A "need" names the specific precondition a piece of blocked work depends on,
# in a normalized, machine-resolvable shape: ``{kind, ref}`` (plus an optional
# human ``label``). ``kind`` is one of NEED_KINDS — a sibling task, a PR, or a
# capability/foundation description. This is the single canonical shape reused
# by BOTH agent blockers (via the status endpoint) and research findings that
# declare a prerequisite. Capturing it changes NO resolution behavior yet — it
# is persisted alongside the existing free-text waiting_on/reason so a later
# resolver can match against it.
NEED_KINDS: tuple[str, ...] = ("task", "pr", "capability")
NEED_REF_MAX_LEN = 200
NEED_LABEL_MAX_LEN = 200

# ── Shared exact-task-ref policy ─────────────────────────────────────────────
# Single source of truth for the ``kind=task`` prerequisite contract, used by
# research prerequisite normalization (``finding_deduplication``), agent
# blocked-need schema/building (``schemas.BlockedNeed`` / :func:`build_need`),
# blocked-need resolution (``blocked_need_resolution``), reaper give-up marking
# (``reaper_sweeps``), and missing-foundation demand aggregation
# (``capability_demand``). A ``task`` reference is EXACT — and therefore a
# machine-resolvable foundation — ONLY when it is a real task UUID or a canonical
# 16-char finding hash (the lowercase-hex prefix ``finding_deduplication.
# finding_hash`` emits). A title-like string — free prose naming a desired
# foundation — is NOT a task ref: it can never resolve to a real task, so it must
# never become durable stranded "missing task" demand. Named foundations belong
# to ``kind=capability`` (plus an explicit ``provides_capability`` marker on the
# provider) instead.
_FINDING_HASH_RE = re.compile(r"^[0-9a-fA-F]{16}$")


def is_exact_task_ref(ref: object) -> bool:
    """True iff ``ref`` is an exact, machine-resolvable task identifier.

    Exact means a real task UUID or a canonical 16-char finding hash — never a
    title-like free-prose string. This is the single predicate the whole
    pipeline shares so a malformed ``kind=task`` ref is rejected identically at
    ingestion, persistence, resolution, give-up marking, and demand aggregation.
    """
    if not isinstance(ref, str):
        return False
    text = ref.strip()
    if not text:
        return False
    if _FINDING_HASH_RE.match(text):
        return True
    try:
        uuid.UUID(text)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def canonicalize_exact_task_ref(ref: object) -> Optional[str]:
    """Return the canonical lowercase form of an exact task ref, or None.

    UUID refs are normalized via ``uuid.UUID`` so mixed-case spellings collapse
    to the stored lowercase UUID string. 16-hex finding hashes are lowercased.
    Non-exact refs return ``None``.
    """
    if not is_exact_task_ref(ref):
        return None
    text = str(ref).strip()
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        return text.lower()


def is_foundation_demand_need(need: object) -> bool:
    """True iff a ``{kind, ref}`` need names a commissionable missing foundation.

    A foundation demand is a ``capability`` need (any non-empty ref — named
    foundations are resolved by explicit provider capability markers) or an
    EXACT ``task`` need (UUID / finding hash). A title-like ``task`` ref names no
    resolvable foundation and is therefore NOT demand — it must never be folded
    into missing-foundation aggregates or stamped as a missing-foundation
    retirement. A ``pr`` need names an external ref and is never foundation
    demand. Malformed input is ``False``.
    """
    if not isinstance(need, dict):
        return False
    kind = need.get("kind")
    ref = need.get("ref")
    if not isinstance(kind, str) or not isinstance(ref, str) or not ref.strip():
        return False
    kind = kind.strip().lower()
    if kind == "capability":
        return True
    if kind == "task":
        return is_exact_task_ref(ref)
    return False


def build_need(
    kind: str, ref: str, label: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return a normalized need dict ``{kind, ref[, label]}`` or ``None``.

    Returns ``None`` when ``kind`` is not a recognized NEED kind or ``ref`` is
    empty after trimming, so callers can pass through unvalidated input and only
    persist a well-formed need. A ``kind=task`` ref that is not exact
    (:func:`is_exact_task_ref` — i.e. a title-like string rather than a task
    UUID / finding hash) is likewise rejected, so a malformed task prerequisite
    is gracefully DROPPED rather than persisted as durable stranded demand;
    callers fall back to the absent-need (free-text) behaviour. Length-bounds
    ``ref`` and ``label``.
    """
    if kind not in NEED_KINDS:
        return None
    ref_text = (ref or "").strip()
    if not ref_text:
        return None
    if kind == "task" and not is_exact_task_ref(ref_text):
        return None
    if kind == "task":
        ref_text = canonicalize_exact_task_ref(ref_text) or ref_text
    need: Dict[str, Any] = {"kind": kind, "ref": ref_text[:NEED_REF_MAX_LEN]}
    if label:
        label_text = str(label).strip()
        if label_text:
            need["label"] = label_text[:NEED_LABEL_MAX_LEN]
    return need


# Default interval before a blocked task is rechecked by the auto-unblock sweep.
DEFAULT_BLOCKER_RECHECK_SECONDS = 600
_UPSTREAM_REASON_MARKERS = (
    "upstream ",
    " rollup",
    "pr #",
    "pull request",
    "not yet merged",
    "dependency",
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _default_blocker_kind(*, waiting_on: str, reason: str, source: str) -> str:
    waiting_norm = (waiting_on or "").lower()
    reason_norm = (reason or "").lower()
    if (
        source == "parent"
        or "subtask" in waiting_norm
        or "dependency" in waiting_norm
        or "upstream" in waiting_norm
        or any(marker in reason_norm for marker in _UPSTREAM_REASON_MARKERS)
    ):
        return "upstream_dependency"
    return "precondition"


def get_blocker_metadata(task: Task) -> Dict[str, Any]:
    """Return a copy of the task's structured blocker metadata (``{}`` if none)."""
    meta = (task.context or {}).get(BLOCKER_CONTEXT_KEY)
    return dict(meta) if isinstance(meta, dict) else {}


def get_blocker_needs(task: Task) -> list:
    """Return the full list of machine-resolvable needs recorded on a blocker.

    A blocked dependent can wait on MORE THAN ONE unresolved prerequisite. The
    canonical store is ``context['blocker']['needs']`` (a list). For backward
    compatibility with rows written before multi-need support — which carry only
    the single ``context['blocker']['need']`` key — this falls back to ``[need]``.
    Returns ``[]`` when the blocker records no machine-resolvable need at all.
    Only well-formed dict entries are returned, so a malformed row degrades to the
    legacy single-need behaviour rather than raising.
    """
    meta = get_blocker_metadata(task)
    raw = meta.get("needs")
    if isinstance(raw, list):
        valid = [n for n in raw if isinstance(n, dict)]
        if valid:
            return valid
    need = meta.get("need")
    if isinstance(need, dict):
        return [need]
    return []


def _raw_need_is_structurally_dead(raw: object) -> bool:
    """Return True for needs that are structurally unresolvable at construction time.

    Two derivations are supported — no caller needs to set an explicit flag:

    1. Explicit flag: ``structurally_dead: True`` in the raw dict.
    2. Observation-class label: when ``label`` (or ``ref``) starts with ``"obs:"``
       (case-insensitive), the need points at an observation finding that never
       spawns a task — no provider can ever exist.

    This is the final construction guard in ``enter_blocked_state``: a caller
    passing a structurally dead need gets CANCELLED+escalated, not BLOCKED.
    """
    if not isinstance(raw, dict):
        return False
    if raw.get("structurally_dead") is True:
        return True
    # Observation-class: "obs: ..." labels can never spawn a provider task.
    for field in ("label", "ref"):
        val = raw.get(field)
        if isinstance(val, str) and val.strip().lower().startswith("obs:"):
            return True
    return False


def is_blocked(task: Task) -> bool:
    """True if the task is in the first-class BLOCKED state."""
    return task.status == TaskStatus.BLOCKED


def enter_blocked_state(
    task: Task,
    *,
    waiting_on: str,
    reason: str,
    source: str = "agent",
    blocker_kind: Optional[str] = None,
    failure_class: str = BLOCKED_PRECONDITION,
    recheck_in_seconds: Optional[int] = DEFAULT_BLOCKER_RECHECK_SECONDS,
    deadline_at: Optional[datetime] = None,
    escalated: bool = False,
    need: Optional[Dict[str, Any]] = None,
    needs: Optional[list] = None,
    publication_gate_receipt: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Transition ``task`` into the BLOCKED state and persist blocker metadata.

    Sets ``task.status = TaskStatus.BLOCKED`` and records what the task is
    waiting on, a human-readable reason, recheck timing, an optional escalation
    deadline, and an escalation marker into ``task.context['blocker']``. Returns
    the metadata dict written. Does NOT increment ``attempt_count`` and does NOT
    record a task/direction failure — BLOCKED is not a failure.

    The caller owns the session (``session.add`` / ``commit``) and any VM
    teardown / account-slot release.
    """
    now = now or _utcnow()
    blocker_code = blocker_kind or _default_blocker_kind(
        waiting_on=waiting_on, reason=reason, source=source
    )
    recheck_at = (
        now + timedelta(seconds=recheck_in_seconds) if recheck_in_seconds else None
    )
    reason_text = (reason or "")[:2000]
    meta: Dict[str, Any] = {
        "waiting_on": waiting_on,
        "reason": reason_text,
        "source": source,
        "blocker_kind": blocker_code,
        "failure_class": failure_class,
        "blocked_at": now.isoformat(),
        "recheck_at": _iso(recheck_at),
        "deadline_at": _iso(deadline_at),
        "escalated": bool(escalated),
    }
    sanitized_receipt = sanitize_publication_gate_receipt(publication_gate_receipt)
    if sanitized_receipt is not None:
        meta[PUBLICATION_GATE_RECEIPT_KEY] = sanitized_receipt
    # Normalized machine-resolvable prerequisite(s). A blocked dependent can wait
    # on MORE THAN ONE unresolved prerequisite, so the canonical store is a LIST
    # under ``meta['needs']``. For backward compatibility we ALSO mirror the first
    # entry into the legacy single ``meta['need']`` key, which existing consumers
    # and already-persisted rows still read (and ``get_blocker_needs`` falls back
    # to). Each entry is re-validated through ``build_need`` so a malformed dict is
    # never persisted; duplicates (same kind+ref) are collapsed. Absent/invalid
    # input leaves the metadata shape exactly as before (no need/needs keys). The
    # ``needs`` list and the single ``need`` argument are both accepted and merged
    # (needs first), so legacy single-need call sites keep working unchanged.
    raw_needs: list = []
    if isinstance(needs, list):
        raw_needs.extend(needs)
    if isinstance(need, dict):
        raw_needs.append(need)
    normalized_needs: list = []
    seen_need_keys: set = set()
    for raw in raw_needs:
        if not isinstance(raw, dict):
            continue
        normalized = build_need(raw.get("kind"), raw.get("ref"), raw.get("label"))
        if normalized is None:
            continue
        key = (normalized["kind"], normalized["ref"])
        if key in seen_need_keys:
            continue
        seen_need_keys.add(key)
        normalized_needs.append(normalized)
    if normalized_needs and all(
        _raw_need_is_structurally_dead(raw) for raw in raw_needs
    ):
        meta["needs"] = normalized_needs
        meta["need"] = normalized_needs[0]
        meta["escalated"] = True
        meta["escalation_detail"] = (
            "structurally unresolvable prerequisite(s) refused at BLOCKED creation"
        )
        ctx = dict(task.context or {})
        ctx[BLOCKER_CONTEXT_KEY] = meta
        ctx["precondition_escalated_to_parent"] = True
        task.context = ctx
        task.status = TaskStatus.CANCELLED
        task.latest_status_message = meta["escalation_detail"]
        return meta
    if normalized_needs:
        meta["needs"] = normalized_needs
        meta["need"] = normalized_needs[0]
    ctx = dict(task.context or {})
    ctx[BLOCKER_CONTEXT_KEY] = meta
    # Preserve BLOCKED_PRECONDITION classification compatibility so historical
    # consumers (digest metrics, analytics, failure taxonomy) keep reading the
    # same failure class even though the task no longer terminates FAILED.
    ctx["failure_category"] = failure_class
    ctx["failure_class"] = failure_class
    task.context = ctx
    task.status = TaskStatus.BLOCKED
    task.blocker_kind = blocker_code[:64]
    task.blocked_reason = reason_text
    task.blocked_at = now
    task.recheck_after = recheck_at
    return meta


def mark_blocker_escalated(
    task: Task, *, detail: Optional[str] = None, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Flag a blocked task's metadata as escalated (e.g. deadline elapsed).

    No-op-safe on tasks that carry no blocker metadata yet — it seeds a minimal
    record so the escalation marker is never lost.
    """
    now = now or _utcnow()
    meta = get_blocker_metadata(task)
    meta["escalated"] = True
    meta["escalated_at"] = now.isoformat()
    if detail:
        meta["escalation_detail"] = detail[:2000]
    ctx = dict(task.context or {})
    ctx[BLOCKER_CONTEXT_KEY] = meta
    task.context = ctx
    return meta


def strip_fresh_attempt_context(
    context: object,
    *,
    preserve_blocked_optimistic_retries: bool = False,
    preserve_non_blocked_failure_verdict: bool = False,
    preserve_context_keys: frozenset[str] | None = None,
) -> Dict[str, Any]:
    """Return ``context`` with stale per-attempt state removed.

    ``preserve_non_blocked_failure_verdict`` exists for
    :func:`clear_blocker_metadata`: that unblock helper historically removes
    only BLOCKED_PRECONDITION compatibility tags, preserving a later
    authoritative verdict such as RATE_LIMIT. Fresh QUEUED resurrection paths
    should use the default and strip all previous verdict keys.
    """
    ctx = dict(context) if isinstance(context, dict) else {}
    preserved_keys = preserve_context_keys or frozenset()
    for key in FRESH_ATTEMPT_CONTEXT_STRIP_KEYS:
        if key in preserved_keys:
            continue
        if key in ("_blocked_optimistic_retries", "_blocked_last_retry_at") and preserve_blocked_optimistic_retries:
            continue
        if key in ("failure_class", "failure_category"):
            if (
                preserve_non_blocked_failure_verdict
                and ctx.get(key) != BLOCKED_PRECONDITION
            ):
                continue
        ctx.pop(key, None)
    return ctx


def restore_requested_payload_model_for_fresh_attempt(
    payloads: object,
    context: object,
) -> bool:
    """Undo the prior dispatch's runtime ``payload.model`` stamp in place.

    New dispatches record both the original request and resolved model in task
    context. Rows created before that provenance existed are left untouched:
    equality with context.llm_model is not enough to distinguish a runtime
    stamp from a genuine operator constraint.
    """

    if not isinstance(payloads, list) or not payloads:
        return False
    payload = payloads[0]
    if not isinstance(payload, dict):
        return False
    ctx = context if isinstance(context, dict) else {}
    current = payload.get("model")
    resolved = ctx.get("_resolved_payload_model")
    requested = ctx.get("_requested_payload_model")
    runtime_stamped = bool(resolved and current == resolved)
    if not runtime_stamped:
        return False
    if isinstance(requested, str) and requested.strip():
        payload["model"] = requested.strip()
    else:
        payload.pop("model", None)
    return True


def clear_blocker_metadata(task: Task) -> None:
    """Drop the structured blocker metadata from the task context.

    Removes the structured ``blocker`` record AND the backward-compat
    ``failure_class`` / ``failure_category`` tags that ``enter_blocked_state``
    wrote — but only when those tags still equal ``BLOCKED_PRECONDITION``. A
    later authoritative verdict (e.g. ``persist_authoritative_verdict`` writing
    ``RATE_LIMIT``) is therefore never clobbered. This guarantees a recovered
    task carries no residual ``BLOCKED_PRECONDITION`` classification (which the
    failure classifier short-circuits on, exempting the work from the retry
    budget) into its next run.

    Also clears reaper escalation markers (``_blocked_escalated``,
    ``_precondition_escalated_key``) so a requeued task never immediately
    re-parks on its next block episode — the escalation was earned by the
    prior run, not the fresh attempt.

    ``_blocked_optimistic_retries`` is included in
    ``FRESH_ATTEMPT_CONTEXT_STRIP_KEYS`` but is deliberately **preserved**
    here via ``preserve_blocked_optimistic_retries=True``: the optimistic
    retry counter accumulates across block episodes within the same task
    lifetime, so it must survive individual unblock transitions (only a full
    fresh-attempt resurrection — e.g. a QUEUED re-spawn — resets it).
    """
    task.blocker_kind = None
    task.blocked_reason = None
    task.blocked_at = None
    task.recheck_after = None
    clear_retry_after(task)
    ctx_src = task.context or {}
    has_blocker = BLOCKER_CONTEXT_KEY in ctx_src
    stale_class = ctx_src.get("failure_class") == BLOCKED_PRECONDITION
    stale_category = ctx_src.get("failure_category") == BLOCKED_PRECONDITION
    has_escalation_markers = (
        "_blocked_escalated" in ctx_src or "_precondition_escalated_key" in ctx_src
    )
    if not (has_blocker or stale_class or stale_category or has_escalation_markers):
        return
    task.context = strip_fresh_attempt_context(
        ctx_src,
        preserve_blocked_optimistic_retries=True,
        preserve_non_blocked_failure_verdict=True,
        preserve_context_keys=authenticated_sidecar_fresh_attempt_preserve_keys(task),
    )


def reset_task_for_fresh_attempt(task: Task) -> None:
    """Clear stale runtime state before a task returns to QUEUED.

    Used by BLOCKED/PARKED resume paths that reuse the same task row. It removes
    the old agent binding and per-attempt liveness markers while preserving
    retry-guidance history that belongs across attempts.
    """
    # Bank before clearing/replacing any live-attempt fields. Some retry callers
    # already bank in order to snapshot guidance; the helper is idempotent after
    # that first call because banking clears the live VM/token evidence.
    bank_task_current_attempt(
        task,
        stopped_at=task.vm_stopped_at or _utcnow(),
    )
    advance_task_lifetime_attempt(task)

    # ``payloads`` is JSONB. Mutating a nested payload in place is not reliably
    # visible to SQLAlchemy, so restore through a copied container and mark the
    # column dirty before the retry is flushed.
    payloads = list(task.payloads or [])
    if payloads and isinstance(payloads[0], dict):
        payloads[0] = dict(payloads[0])
    if restore_requested_payload_model_for_fresh_attempt(payloads, task.context):
        task.payloads = payloads
        flag_modified(task, "payloads")
    # Research completion consumes ``plan_output`` as attempt-local agent output
    # and uses finalized/spawn-pending context markers as idempotency shortcuts.
    # Reusing either on a true requeue can no-op the new attempt or respawn work
    # from the prior one. Other task types retain their existing output
    # semantics.
    if (task.first_payload or {}).get("type") == "research":
        task.plan_output = None
    _archive_current_publication_gate_receipt(task, superseded_by="fresh_attempt")
    clear_blocker_metadata(task)
    task.last_heartbeat = None
    task.started_at = None
    task.vm_instance_id = None
    task.parked_at = None
    task.park_reason = None
    task.llm_account_id = None
    clear_retry_after(task)
    task.context = {
        **strip_fresh_attempt_context(
            task.context or {},
            preserve_context_keys=authenticated_sidecar_fresh_attempt_preserve_keys(
                task
            ),
        ),
        "_fresh_attempt_started_at": _utcnow().isoformat(),
    }


def unblock_to_queued(task: Task, *, message: Optional[str] = None) -> None:
    """Out-transition: return a BLOCKED task to QUEUED once its precondition
    resolves.

    Clears the blocker metadata and re-queues the task for dispatch. The status
    change fires the model's revive callbacks (see models._on_status_set), which
    clear any JWT deny-list entry left by the BLOCKED transition. The sweep that
    decides *when* a precondition has resolved is owned by the auto-unblock work.
    """
    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    task.latest_status_message = (
        message or "Unblocked — precondition resolved, requeued"
    )
