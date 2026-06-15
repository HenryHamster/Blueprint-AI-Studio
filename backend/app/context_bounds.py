"""Reusable size-bounding for unbounded ``task.context`` JSONB sub-fields.

A single-user production deployment suffered a Postgres saturation outage traced
to structural fat-row growth in the ``tasks`` table: a task's ``context`` JSONB
grew to multiple megabytes because the review->fix loop accumulates a
per-iteration ``review_history`` in which each entry stores the reviewer's full
freeform feedback. On the fallback path where the reviewer agent emits no
structured verdict, that stored feedback is the agent's ENTIRE run log. The blob
is carried forward and re-serialized into every downstream task's ``context`` on
every spawn — and because ``context`` is GIN-indexed, each write triggers a TOAST
rewrite plus a GIN reindex, producing the multi-second single-row primary-key
updates that saturated Postgres, tripped Cloud Run liveness probes, and caused
leader thrash.

``payloads`` already has both an app-layer cap (``pack_under_budget``, 120 KB)
and a DB cap (``chk_task_payloads_size``, the 200 KB CHECK from alembic 0063);
``context`` had no equivalent guard on the *pipeline write path* — only the
admin-mutation path checked a size limit (admin_task_mutations.py). This module
is the missing write-path guard, factored as a reusable primitive so other
unbounded ``context`` sub-fields bound the same way rather than each
re-inventing a one-field patch.

It mirrors the playbook capacity pattern
(``playbook_capacity.maybe_archive_for_capacity``) — keep-most-recent eviction
plus a size guard, applied *before* the write — but is list-shaped rather than
playbook-dict-shaped.

Adopting elsewhere
------------------
``bound_history_entries`` is general. Two modes:

* **Count-capped + text-truncated** (``max_entries`` set) — for append-only
  *history* where old entries are disposable. ``bound_review_history`` is the
  review_history wrapper.
* **Truncate-only with count backstop** — for accumulations whose entries are
  *work items* that must NOT be dropped (dropping would silently lose queued
  work), where only oversized freeform text needs bounding. The research
  ``deferred_findings`` accumulation uses :func:`bound_deferred_findings`, a
  convenience wrapper that truncates ``title``, ``description``, and ``body``
  to ``DEFERRED_FINDING_MAX_CHARS`` and applies a generous count backstop
  (``DEFERRED_FINDINGS_MAX_ENTRIES``) that only fires for pathologically stalled
  directions::

      ctx["deferred_findings"] = bound_deferred_findings(all_deferred)
"""

import json
import logging

logger = logging.getLogger(__name__)

# ── review_history bounds ─────────────────────────────────────────────────────
# Max review_history entries carried in review_loop. The review-fix loop caps at
# MAX_REVIEW_FIX_ITERATIONS (16) REQUEST_CHANGES iterations, each of which may
# also emit an APPROVE entry at the CI-wait transition, so even a pathological
# healthy chain tops out near ~32 entries. 50 leaves headroom above that real
# ceiling while still bounding unbounded accumulation — the carried-forward list
# is otherwise only ever appended to, never compacted.
REVIEW_HISTORY_MAX_ENTRIES = 50

# Max chars of reviewer freeform feedback persisted per review_history entry.
# Legitimate structured feedback is a few hundred to a couple thousand chars; the
# fat-row pathology is the log-scraping fallback storing the agent's ENTIRE run
# log (observed multi-MB). 8 KB cleanly separates the two: every realistic
# summary is below it (so truncation is a no-op for normal feedback), while a
# runaway log is cut by >99%. Kept comfortably above every downstream feedback
# reader — the retry_prompt preview slices <=800 chars, and the stuck-cycle
# Jaccard fallback compares token sets that, for any real-world-length feedback,
# are fully contained in the first 8 KB — so neither preview text nor similarity
# changes for realistic inputs.
REVIEW_FEEDBACK_MAX_CHARS = 8192

# Per-entry text cap a research ``deferred_findings`` adopter would use (see the
# module docstring). Defined here so the bound lives with its siblings; wiring is
# a one-line change at the research write site.
DEFERRED_FINDING_MAX_CHARS = 8192

# Generous count-cap backstop for deferred_findings. Findings are work items
# (dropping silently would lose queued work), so the primary bound is text-only.
# This backstop prevents pathological count growth (stalled directions that
# accumulate hundreds of perpetually-unresolvable entries) from itself becoming a
# fat row: 200 entries * 8 KB = ~1.6 MB freeform text worst-case. Set well above
# any realistic single-cycle maximum (~10–30 per cycle) so the cap is never
# reached under normal operation. When it fires, the oldest entries are dropped
# (they have been deferred the most times and are most stale) and the eviction is
# logged explicitly — never silent.
DEFERRED_FINDINGS_MAX_ENTRIES = 200

# Soft telemetry threshold: a persisted ``context`` larger than this is logged as
# a possible fat-row regression. Set above the bound's own worst case
# (REVIEW_HISTORY_MAX_ENTRIES * REVIEW_FEEDBACK_MAX_CHARS ~= 400 KB of feedback
# plus structured fields) so a bounded context never false-alarms, while the
# multi-MB pathology that caused the outage is surfaced rather than silent.
CONTEXT_SOFT_WARN_BYTES = 1024 * 1024

# Elision marker appended to a truncated text field so a reader (human or the
# similarity check) can tell the value was cut rather than naturally short.
_TRUNCATION_MARKER = "…[truncated]"


def _truncate_text(value: object, max_chars: int) -> object:
    """Return *value* truncated to *max_chars* with an elision marker, or
    unchanged if it is not an over-length string.

    Non-str values and short strings pass through untouched, so structured
    fields are never coerced and below-cap feedback is byte-identical to the
    input (the common case — truncation is a no-op for realistic feedback).
    """
    if not isinstance(value, str) or len(value) <= max_chars:
        return value
    if max_chars <= len(_TRUNCATION_MARKER):
        # Cap is smaller than the marker itself — hard-truncate without it so the
        # result still honours max_chars. (Never hit in practice; the real caps
        # are 8 KB, far above the marker.)
        return value[:max_chars]
    keep = max_chars - len(_TRUNCATION_MARKER)
    return value[:keep] + _TRUNCATION_MARKER


def bound_history_entries(
    entries: "list | None",
    *,
    text_fields: "tuple[str, ...]",
    max_field_chars: int,
    max_entries: "int | None" = None,
) -> list:
    """Return a size-bounded copy of *entries* (a list of dicts).

    * Keeps the most-recent *max_entries* when set (``None`` keeps all — use this
      for work-queue accumulations where dropping an entry would lose work).
    * Truncates each ``text_fields`` value on each kept entry to *max_field_chars*
      with an elision marker. Every other field — structured ``issues``,
      ``changed_files``, ``finding_hash``, ``verdict``, ``iteration`` — is copied
      verbatim, so consumers that read structured data are unaffected.

    Never mutates the input: each kept entry is shallow-copied before its text
    fields are rewritten. Empty / ``None`` safe. Non-dict entries pass through
    untouched (defensive — review_history is always list-of-dict in practice).
    """
    if not entries:
        return []
    if max_entries is None:
        kept = list(entries)
    elif max_entries <= 0:
        kept = []
    else:
        kept = list(entries[-max_entries:])
    bounded: list = []
    for entry in kept:
        if not isinstance(entry, dict):
            bounded.append(entry)
            continue
        new_entry = dict(entry)
        for field in text_fields:
            if field in new_entry:
                new_entry[field] = _truncate_text(new_entry[field], max_field_chars)
        bounded.append(new_entry)
    return bounded


def bound_review_history(entries: "list | None") -> list:
    """Bound ``review_loop['review_history']`` for ``context`` persistence.

    Caps the entry count and truncates each entry's freeform ``feedback`` to the
    review-history constants. Convenience wrapper so every write path (the
    review_pipeline spawn store and the retry_prompt APPROVE append) bounds
    identically. Re-bounds the WHOLE list, not just a new entry, so a fat blob
    carried over from a pre-fix in-flight row self-heals on the next spawn.
    """
    return bound_history_entries(
        entries,
        text_fields=("feedback",),
        max_field_chars=REVIEW_FEEDBACK_MAX_CHARS,
        max_entries=REVIEW_HISTORY_MAX_ENTRIES,
    )


def bound_deferred_findings(entries: "list | None") -> list:
    """Bound ``task.context['deferred_findings']`` for context persistence.

    Truncates freeform text fields (title, description, body) to
    DEFERRED_FINDING_MAX_CHARS with an elision marker. Structured routing fields
    (finding_hash, type, depends_on, prerequisite, deferred_reason, deferred_count,
    etc.) are preserved verbatim, so dedup and routing consumers are unaffected.

    Findings are work items — dropping silently would lose queued work — so the
    primary guard is text-only (max_entries=None by default). A generous count
    backstop (DEFERRED_FINDINGS_MAX_ENTRIES) evicts the OLDEST entries when the
    list grows pathologically; the eviction is logged explicitly, never silent.
    """
    if not entries:
        return []
    if len(entries) > DEFERRED_FINDINGS_MAX_ENTRIES:
        n_dropped = len(entries) - DEFERRED_FINDINGS_MAX_ENTRIES
        logger.warning(
            "[CONTEXT-BOUNDS] deferred_findings count cap: %d entries dropped "
            "(kept %d most-recent, cap=%d) — possible stalled direction",
            n_dropped,
            DEFERRED_FINDINGS_MAX_ENTRIES,
            DEFERRED_FINDINGS_MAX_ENTRIES,
        )
    return bound_history_entries(
        entries,
        text_fields=("title", "description", "body"),
        max_field_chars=DEFERRED_FINDING_MAX_CHARS,
        max_entries=DEFERRED_FINDINGS_MAX_ENTRIES,
    )


def context_size_bytes(context: "dict | None") -> int:
    """JSON-serialised byte size of a task ``context`` dict — cheap telemetry so
    a future fat-row regression is measurable. ``default=str`` keeps it total
    over non-JSON-native values (datetimes, etc.); returns 0 on any failure so
    telemetry never raises into a write path.
    """
    if not context:
        return 0
    try:
        return len(json.dumps(context, default=str).encode())
    except (TypeError, ValueError):
        return 0


def warn_if_context_oversized(
    context: "dict | None",
    *,
    label: str,
    soft_limit_bytes: int = CONTEXT_SOFT_WARN_BYTES,
) -> int:
    """Log a WARNING when a persisted ``context`` exceeds *soft_limit_bytes* and
    return its measured byte size. Cheap, non-raising telemetry so a fat-row
    regression is visible in logs rather than only discoverable post-outage.
    """
    size = context_size_bytes(context)
    if size > soft_limit_bytes:
        logger.warning(
            "[CONTEXT-SIZE] %s: persisted task.context is %d bytes "
            "(> %d soft cap) — possible fat-row regression",
            label,
            size,
            soft_limit_bytes,
        )
    return size
