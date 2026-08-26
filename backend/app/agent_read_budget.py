"""Shared serialized-byte budget for the agent-facing pipeline-visibility reads.

Every read surface the Strategic Resident can pull with ``./fetch`` lands in
the resident's context and, from there, in the strategic memory envelope. That
envelope degrades into a coreless stub above
``strategic_memory_envelope.MAX_CANONICAL_ENVELOPE_BYTES`` (64 KiB) — the
condition behind the infinite reclaim livelock root-fixed by PR #17424. An
unbounded "just return everything" projection is therefore not merely verbose,
it is a livelock hazard.

So each visibility surface is capped at a QUARTER of the envelope ceiling: no
single read may consume more than a quarter of the budget the envelope has for
everything. That is the property this module enforces, and it is the only one.

What this module does NOT guarantee
-----------------------------------
``AGENT_READ_MAX_RESPONSE_BYTES * 4 == MAX_CANONICAL_ENVELOPE_BYTES`` is a
statement about one read's blast radius, **not** a promise that four reads fit
in an envelope. It cannot be: the envelope also holds the canonical core, the
resident can issue more than four reads in a turn, and nothing at this layer
counts reads. Lowering the cap to buy nominal headroom would move that
arithmetic without making it a guarantee, at the cost of real answer quality —
so the cap is left where it is and the claim is stated honestly instead. What
actually keeps an assembled envelope under the threshold is the degradation
ladder in :mod:`app.strategic_memory_envelope`; the coupling to
``MAX_CANONICAL_ENVELOPE_BYTES`` here is a shared reference point (same hazard,
same order of magnitude), not a shared mechanism — agent ``./fetch`` responses
never pass through the canonical-core assembler.

What :func:`clamp_read_response` actually guarantees
----------------------------------------------------
It sheds rows from the list-valued keys named in ``trim_order``, cheapest
signal first, until the exact transport encoding fits. It cannot shrink
anything else — aggregate maps in particular are deliberately never trimmed,
because a census that has lost its detail rows is still a usable census while
one that has lost its counts is not. A surface whose *aggregates alone* exceed
the ceiling therefore comes back over budget, and when that happens the
response says so: ``bounds.over_budget`` is ``True`` and the event is logged.

That makes bounding aggregate cardinality the caller's job, and
:func:`bound_counts` is the tool for it: top-N keys by weight plus an ``other``
bucket, with totals preserved exactly. Any surface emitting a map whose key
vocabulary is not constrained by an enum or a DB check constraint must run it
through :func:`bound_counts` — free-string keys (a task ``type`` read out of
payload JSONB, for instance) have structurally unbounded cardinality, and
roughly 300 distinct keys breach the cap with zero detail rows present.

Responses shaped by :func:`clamp_read_response` always carry a ``bounds`` block
naming the byte ceiling, whether anything was dropped to reach it, and whether
the ceiling was actually met, so the reader can tell a complete answer from a
truncated one — and a truncated one from a failed bound.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from app.strategic_memory_envelope import MAX_CANONICAL_ENVELOPE_BYTES

logger = logging.getLogger(__name__)

# One visibility surface may occupy at most a quarter of the strategic envelope.
# Pinned by testing/test_agent_pipeline_visibility.py — do not raise this without
# re-deriving it from MAX_CANONICAL_ENVELOPE_BYTES.
AGENT_READ_MAX_RESPONSE_BYTES = MAX_CANONICAL_ENVELOPE_BYTES // 4

# Per-field text ceiling applied before the whole-response clamp. Free text
# (titles, status messages, direction text, log samples) is where these
# projections actually grow, so it is trimmed first and visibly.
AGENT_READ_MAX_TEXT_BYTES = 240

# Headroom withheld from the trim budget for the ``bounds`` block itself. The
# block is written after trimming, so without a reserve a response clamped to
# exactly the ceiling would be pushed past it by its own accounting.
AGENT_READ_BOUNDS_RESERVE_BYTES = 256

# Aggregate-map bounds. A count map's keys are as untrimmable as its values, so
# ``bound_counts`` caps both the key vocabulary and the key length. Deliberately
# far tighter than AGENT_READ_MAX_TEXT_BYTES: 20 keys of 240 bytes would be
# ~5 KiB of key text alone, which defeats the point of bounding at all.
AGENT_READ_AGGREGATE_TOP_N = 20
AGENT_READ_MAX_KEY_BYTES = 64
AGENT_READ_OTHER_KEY = "other"


def serialized_bytes(value: Any) -> int:
    """Byte length of *value* in the exact encoding Starlette will transmit."""

    return len(
        json.dumps(
            value, separators=(",", ":"), default=str, ensure_ascii=False
        ).encode("utf-8")
    )


def clip_text(value: Any, *, max_bytes: int = AGENT_READ_MAX_TEXT_BYTES) -> str | None:
    """Trim free text to *max_bytes* at a UTF-8 boundary; ``None`` stays ``None``."""

    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "…"


def _count_weight(value: Any) -> int:
    """Total occurrences a count-map value represents.

    Handles the two shapes these projections emit: a flat ``{key: int}`` and a
    nested ``{key: {sub_key: int}}`` (task type broken down by status).
    """

    if isinstance(value, bool):  # bool is an int subclass, and is not a count
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        return sum(_count_weight(item) for item in value.values())
    return 0


def _merge_counts(into: Any, value: Any) -> Any:
    """Add *value* into *into*, preserving flat/nested shape and exact totals."""

    if into is None:
        if isinstance(value, Mapping):
            return {key: _merge_counts(None, item) for key, item in value.items()}
        return _count_weight(value)
    if isinstance(into, Mapping) and isinstance(value, Mapping):
        merged = dict(into)
        for key, item in value.items():
            merged[key] = _merge_counts(merged.get(key), item)
        return merged
    # Shape clash (a flat count folded into a nested one, or the reverse).
    # Degrade to a scalar rather than invent a sub-key: the total is what has
    # to survive, and a made-up sub-key would be a lie rather than a loss.
    return _count_weight(into) + _count_weight(value)


def bound_counts(
    counts: Any,
    *,
    top_n: int = AGENT_READ_AGGREGATE_TOP_N,
    other_key: str = AGENT_READ_OTHER_KEY,
    max_key_bytes: int = AGENT_READ_MAX_KEY_BYTES,
) -> dict[str, Any]:
    """Bound an aggregate count map to *top_n* keys plus an ``other`` bucket.

    :func:`clamp_read_response` cannot shrink a count map, so a map keyed by a
    free string — a task ``type`` read out of payload JSONB, an unconstrained
    reason code — is an unbounded response no matter what the clamp does. This
    is what bounds it, and applying it is the caller's responsibility.

    Three separate bounds, all load-bearing:

    * **Cardinality** — keys are ranked by total weight (descending, then by
      key so ties are deterministic) and everything past *top_n* is folded into
      ``other_key``.
    * **Key length** — every key is clipped to *max_key_bytes*. Keys that
      collide after clipping merge, which keeps the total exact.
    * **Totals** — the sum over the returned map equals the sum over the input,
      for both the flat ``{key: int}`` and the nested ``{key: {sub: int}}``
      shape. The bucket is a fold, not a drop, so the census stays
      arithmetically honest even when most of its vocabulary is hidden.

    A key that is literally ``other_key`` merges with the folded remainder
    rather than being shadowed by it — again, so the total stays exact.
    """

    if not isinstance(counts, Mapping):
        return {}

    keep = max(0, top_n)
    clipped: dict[str, Any] = {}
    for raw_key, value in counts.items():
        key = clip_text(raw_key, max_bytes=max_key_bytes)
        if key is None:
            key = "unknown"
        if key in clipped:
            clipped[key] = _merge_counts(clipped[key], value)
        else:
            clipped[key] = _merge_counts(None, value)

    if len(clipped) <= keep:
        return clipped

    ranked = sorted(clipped.items(), key=lambda kv: (-_count_weight(kv[1]), kv[0]))
    bounded: dict[str, Any] = dict(ranked[:keep])
    for _folded_key, value in ranked[keep:]:
        bounded[other_key] = _merge_counts(bounded.get(other_key), value)
    return bounded


def clamp_read_response(
    response: dict[str, Any],
    *,
    trim_order: tuple[str, ...],
    max_bytes: int = AGENT_READ_MAX_RESPONSE_BYTES,
    returned_counts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Force *response* under *max_bytes* where it can, and say so when it cannot.

    ``trim_order`` names the list-valued keys that may be shortened, cheapest
    signal first. Each named list is halved (then emptied) in turn until the
    serialized response fits. Aggregate counts are never trimmed — a census
    that has lost its detail rows is still a usable census, whereas one that
    has lost its counts is not.

    That makes the guarantee conditional, and it is stated as such: if emptying
    every key in ``trim_order`` still leaves the response over the ceiling —
    because what remains is aggregates, scalars, or keys nobody named — the
    response comes back oversized with ``bounds.over_budget`` set to ``True``
    and a warning logged. Bounding the untrimmable part is the caller's job
    (:func:`bound_counts`); ``over_budget`` exists so a surface that grows a new
    un-trimmable key fails loudly instead of silently, the way an unbounded
    ``non_terminal_by_type`` map once returned 2x the cap while reporting only
    that it had truncated.

    Trimming targets ``max_bytes`` minus :data:`AGENT_READ_BOUNDS_RESERVE_BYTES`
    so the ``bounds`` block written afterwards cannot push the response back
    over the ceiling it was just trimmed to fit.

    ``returned_counts`` maps a ``bounds`` key to a response list key; each is
    recorded as that list's post-trim length. Recording them here rather than
    at the call site keeps ``response_bytes`` exact, since nothing is added to
    the payload after it is measured.

    The returned dict always carries ``bounds``: ``max_response_bytes``,
    ``response_bytes``, ``truncated``, ``over_budget``, and (when truncated)
    ``truncated_sections``.
    """

    budget = max(0, max_bytes - AGENT_READ_BOUNDS_RESERVE_BYTES)
    truncated_sections: list[str] = []
    for key in trim_order:
        if serialized_bytes(response) <= budget:
            break
        rows = response.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        while rows and serialized_bytes(response) > budget:
            rows = rows[: len(rows) // 2]
            response[key] = rows
        if key not in truncated_sections:
            truncated_sections.append(key)

    # The trim order is exhausted; whatever is left is untrimmable here. Record
    # the outcome rather than letting a miss read as a success.
    remaining = serialized_bytes(response)
    over_budget = remaining > budget
    if over_budget:
        logger.warning(
            "[AGENT_READ] response still over budget after exhausting "
            "trim_order=%s: %d bytes vs %d budget (%d cap). Untrimmable keys: "
            "%s. An aggregate map here needs bound_counts().",
            list(trim_order),
            remaining,
            budget,
            max_bytes,
            sorted(key for key in response if key not in trim_order),
        )

    bounds = response.setdefault("bounds", {})
    if isinstance(bounds, dict):
        bounds["max_response_bytes"] = max_bytes
        bounds["truncated"] = bool(truncated_sections)
        # Always present, not only when it fires: a positive statement that the
        # ceiling was actually met is exactly what a reader could not get
        # before, since ``truncated`` says only that trimming happened.
        bounds["over_budget"] = over_budget
        if truncated_sections:
            bounds["truncated_sections"] = truncated_sections
        for bound_key, list_key in (returned_counts or {}).items():
            rows = response.get(list_key)
            bounds[bound_key] = len(rows) if isinstance(rows, list) else 0
        # Fixed point: the measurement is itself part of the payload, so widen
        # it until the recorded number matches the encoded length exactly.
        bounds["response_bytes"] = 0
        for _ in range(4):
            measured = serialized_bytes(response)
            if bounds["response_bytes"] == measured:
                break
            bounds["response_bytes"] = measured
    return response
