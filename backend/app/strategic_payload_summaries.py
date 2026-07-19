"""Strategic-payload field summarizers and open-message inbox helpers.

Cohesive cluster extracted verbatim from ``strategic_spawning.py`` to keep that
module under its size cap. These pure summarize/floor callbacks degrade the
strategist payload sections under byte pressure (conversation thread, memo
citations, operator-context refs, open-message inbox, and the long-tail
digest sections) while preserving the identifying pointers, plus the two DB
fetchers that build the operator-context and open-message feeds.

Every name defined here is re-exported from ``app.strategic_spawning`` so all
existing ``from app.strategic_spawning import ...`` call sites and tests keep
resolving unchanged.
"""

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MessageType, ProjectMessage
from app.project_message_projection import agent_thread_message_dict
from app.strategic_payload_compaction import (
    _CONVERSATION_THREAD_KEEP_WHEN_TRIMMED,
    _bounded_text,
)


def _summarize_conversation_thread(thread: list) -> list:
    """Keep the most recent N conversation messages."""
    if (
        not isinstance(thread, list)
        or len(thread) <= _CONVERSATION_THREAD_KEEP_WHEN_TRIMMED
    ):
        return thread
    return thread[-_CONVERSATION_THREAD_KEEP_WHEN_TRIMMED:]


_MEMO_CITATIONS_KEEP = 5
_MEMO_EXCERPT_MAX_CHARS = 400


_MEMO_HEAVY_FIELDS = ("citations", "final_metrics")
_MEMO_VERBOSE_FIELDS = ("rationale", "reasoning")


def _strip_verbose_fields(m: dict) -> dict:
    """Return a copy without heavy AND verbose optional fields."""
    drop = set(_MEMO_HEAVY_FIELDS) | set(_MEMO_VERBOSE_FIELDS)
    return {k: v for k, v in m.items() if k not in drop}


def _cap_excerpt(m: dict) -> dict:
    result = dict(m)
    result["summary_excerpt"] = (m.get("summary_excerpt") or "")[
        :_MEMO_EXCERPT_MAX_CHARS
    ]
    return result


def _summarize_memo_citations(memos: list) -> list:
    """Trim memo citations for inclusion in strategic-agent payloads.

    Field-level trimming order (applied before entry-count capping):
      1. Drop ``citations`` and ``final_metrics`` (largest optional fields).
      2. Drop ``rationale`` and ``reasoning`` (verbose text fields; compact
         ``*_excerpt`` variants are preserved).
      3. Cap ``summary_excerpt`` to ``_MEMO_EXCERPT_MAX_CHARS``.
    Then cap entry count to ``_MEMO_CITATIONS_KEEP`` (newest-first).

    This ensures over-budget payloads degrade to a compact citable digest
    rather than dropping the whole feed. ``task_id`` is always preserved.
    """
    if not isinstance(memos, list):
        return memos
    result = []
    for m in memos:
        if not isinstance(m, dict):
            result.append(m)
            continue
        entry = _strip_verbose_fields(m)
        entry = _cap_excerpt(entry)
        result.append(entry)
    return result[:_MEMO_CITATIONS_KEEP]


# ── Open-message inbox (the strategist's authoritative feed) ──────────────────
# The strategist's open-message inbox is its OWN status-filtered query — NOT a
# slice of the capped conversation_thread. Without this, an OPEN BUG_REPORT or
# BLOCKER older than the 15 newest messages ages out of the thread window and
# the strategist never sees it, so it can never be acted on or resolved and
# stays open forever. The feed is size-bounded two ways (a hard message count +
# a per-message excerpt cap) and priority-ordered so unresolved
# BUG_REPORT/BLOCKER items (then the oldest-open items) are the LAST to be
# dropped under budget pressure, never the first.
_OPEN_MESSAGES_FETCH_LIMIT = 200  # DB window over open-status rows
_OPEN_MESSAGES_KEEP = 40  # count bound for the packed feed
_OPEN_MESSAGES_KEEP_WHEN_TRIMMED = 20  # tighter bound for the summarize fallback
_OPEN_MESSAGE_EXCERPT_MAX_CHARS = 800  # per-message content excerpt cap
_CRITICAL_OPEN_MESSAGE_TYPES = (
    MessageType.BUG_REPORT.value,
    MessageType.BLOCKER.value,
)
# Same critical types as ORM enum members, for DB-side ordering (the tuple above
# holds string ``.value``s used against the projected message dicts).
_CRITICAL_OPEN_MESSAGE_DB_TYPES = (
    MessageType.BUG_REPORT,
    MessageType.BLOCKER,
)
_OPERATOR_CONTEXT_MESSAGE_FETCH_LIMIT = 30
_OPERATOR_CONTEXT_REQUESTS_KEEP = 6
_OPERATOR_CONTEXT_REFS_KEEP = 12
_OPERATOR_CONTEXT_REF_SCAN_LIMIT = 24
_OPERATOR_CONTEXT_DESCRIPTOR_SCAN_LIMIT = 24
_OPERATOR_CONTEXT_FROM_TYPES = ("user", "operator")
_OPERATOR_CONTEXT_TARGET_KEYS = (
    "surface",
    "section",
    "task_id",
    "message_id",
    "direction_id",
    "document_id",
    "artifact_id",
    "cycle_id",
)


def _open_message_survival_key(msg: dict) -> tuple:
    """Sort key ranking an open message's survival priority HIGH→LOW.

    Critical types (BUG_REPORT/BLOCKER) rank first, with reproducing (non-stale)
    ones ahead of stale (citation_state == "absent") ones; within each tier,
    oldest-open items rank first (ascending created_at). The HEAD of the sorted
    list survives when the feed must be bounded, so reproducing criticals are
    last to be dropped, followed by stale criticals, then non-critical messages.
    """
    is_critical = msg.get("type") in _CRITICAL_OPEN_MESSAGE_TYPES
    citation_state = msg.get("citation_state")
    is_stale = citation_state == "absent"

    # Tier 0: reproducing critical messages (highest priority)
    # Tier 1: stale critical messages (lower than reproducing)
    # Tier 2: non-critical messages
    if is_critical and not is_stale:
        tier = 0
    elif is_critical and is_stale:
        tier = 1
    else:
        tier = 2

    return (tier, msg.get("created_at") or "")


def _bound_open_messages(messages: list, keep: int) -> list:
    """Bound *messages* to *keep* items and cap each message's excerpt length.

    Lowest-priority survivors are dropped first: BUG_REPORT/BLOCKER (then the
    oldest-open) items are the LAST to go. Survivors are returned in oldest-open
    chronological order so the strategist reads a stable inbox regardless of
    which items were trimmed.
    """
    if not isinstance(messages, list):
        return messages
    capped: list = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            content = m["content"]
            if len(content) > _OPEN_MESSAGE_EXCERPT_MAX_CHARS:
                m = {**m, "content": content[:_OPEN_MESSAGE_EXCERPT_MAX_CHARS]}
        capped.append(m)
    if len(capped) <= keep:
        return capped
    survivors = sorted(
        (m for m in capped if isinstance(m, dict)),
        key=_open_message_survival_key,
    )[:keep]
    # Re-sort survivors chronologically (oldest-open first) for a stable read.
    survivors.sort(key=lambda m: m.get("created_at") or "")
    return survivors


def _summarize_open_messages(messages: list) -> list:
    """Budget-pressure fallback for the open-message inbox.

    Rather than dropping the entire inbox wholesale when the strategic payload
    is over budget, trim it to a tighter bound that PRESERVES unresolved
    BUG_REPORT/BLOCKER (and the oldest-open) items and drops the rest — so the
    critical inbox items survive budget pressure instead of vanishing.
    """
    return _bound_open_messages(messages, _OPEN_MESSAGES_KEEP_WHEN_TRIMMED)


# Guaranteed-minimum core floor for the open-message inbox: tighter than the
# summarize fallback (fewer items, shorter excerpts) so the operator inbox
# NEVER disappears entirely under extreme byte pressure. Fuller content is
# fetchable on demand; the survival ordering keeps reproducing BUG_REPORT/
# BLOCKER (then oldest-open) items as the last to be trimmed.
_OPEN_MESSAGES_FLOOR_KEEP = 8
_OPEN_MESSAGE_FLOOR_EXCERPT_MAX_CHARS = 200


def _floor_open_messages(messages: list) -> list:
    """Core floor callback for the ``open_messages`` pack key.

    Keeps only the highest-survival-priority inbox items with a tight excerpt
    cap so the strategist always sees the operator-inbox core even when the
    fuller feed cannot fit. Distinct from :func:`_summarize_open_messages`,
    which keeps a larger window — this is the last-resort minimum the packer
    reserves off the top of the budget.
    """
    bounded = _bound_open_messages(messages, _OPEN_MESSAGES_FLOOR_KEEP)
    if not isinstance(bounded, list):
        return bounded
    floored: list = []
    for m in bounded:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            content = m["content"]
            if len(content) > _OPEN_MESSAGE_FLOOR_EXCERPT_MAX_CHARS:
                m = {**m, "content": content[:_OPEN_MESSAGE_FLOOR_EXCERPT_MAX_CHARS]}
        floored.append(m)
    return floored


def _operator_context_text(
    value: Any,
    *,
    max_chars: int,
    raw_max_chars: int | None = None,
) -> str | None:
    if not isinstance(value, str):
        return None
    raw_max = raw_max_chars if raw_max_chars is not None else max_chars * 4
    if len(value) > raw_max:
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    return text[:max_chars]


def _operator_context_ref(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    kind = _operator_context_text(raw.get("kind"), max_chars=40, raw_max_chars=160)
    ref_id = _operator_context_text(raw.get("id"), max_chars=160, raw_max_chars=640)
    if not kind or not ref_id:
        return None
    ref: dict[str, str] = {"kind": kind, "id": ref_id}
    label = _operator_context_text(
        raw.get("label"), max_chars=180, raw_max_chars=720
    )
    if label:
        ref["label"] = label
    return ref


def _operator_context_target(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    target: dict[str, str] = {}
    for key in _OPERATOR_CONTEXT_TARGET_KEYS:
        value = _operator_context_text(
            raw.get(key), max_chars=180, raw_max_chars=720
        )
        if value:
            target[key] = value
    return target or None


def _operator_context_descriptor(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = _operator_context_text(raw.get("kind"), max_chars=40, raw_max_chars=160)
    ref_id = _operator_context_text(raw.get("id"), max_chars=160, raw_max_chars=640)
    if not kind or not ref_id:
        return None

    descriptor: dict[str, Any] = {"kind": kind, "id": ref_id}
    label = _operator_context_text(
        raw.get("label"), max_chars=180, raw_max_chars=720
    )
    if label:
        descriptor["label"] = label
    summary = _operator_context_text(
        raw.get("summary"), max_chars=220, raw_max_chars=880
    )
    if summary:
        descriptor["summary"] = summary
    status = _operator_context_text(
        raw.get("status"), max_chars=80, raw_max_chars=320
    )
    if status:
        descriptor["status"] = status
    target = _operator_context_target(raw.get("target"))
    if target:
        descriptor["target"] = target
    return descriptor


def _operator_context_descriptor_map(
    snapshot: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    raw_descriptors = snapshot.get("selected_ref_descriptors")
    if not isinstance(raw_descriptors, list):
        return {}
    descriptors: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in raw_descriptors[:_OPERATOR_CONTEXT_DESCRIPTOR_SCAN_LIMIT]:
        descriptor = _operator_context_descriptor(raw)
        if not descriptor:
            continue
        key = (descriptor["kind"], descriptor["id"])
        if key not in descriptors:
            descriptors[key] = descriptor
    return descriptors


def _operator_context_package_descriptor(raw: Any) -> dict[str, Any] | None:
    descriptor = _operator_context_descriptor(raw)
    if descriptor:
        return descriptor
    if not isinstance(raw, dict):
        return None
    package_id = _operator_context_text(
        raw.get("id") or raw.get("context_package_id"),
        max_chars=180,
        raw_max_chars=720,
    )
    if not package_id:
        return None
    result: dict[str, Any] = {"id": package_id}
    label = _operator_context_text(
        raw.get("label") or raw.get("title"),
        max_chars=180,
        raw_max_chars=720,
    )
    if label:
        result["label"] = label
    summary = _operator_context_text(
        raw.get("summary"), max_chars=220, raw_max_chars=880
    )
    if summary:
        result["summary"] = summary
    status = _operator_context_text(
        raw.get("status"), max_chars=80, raw_max_chars=320
    )
    if status:
        result["status"] = status
    return result


def _operator_context_created_sort_value(message: ProjectMessage) -> tuple:
    created_at = message.created_at or datetime.min
    return (created_at, str(message.id))


def _build_operator_context_refs_from_messages(
    messages: list[ProjectMessage],
) -> list[dict[str, Any]]:
    """Promote recent operator-selected Strategic context into pointer entries.

    The source snapshots were already sanitized when ProjectMessage rows were
    created. This bridge still treats the JSON defensively because rows may be
    legacy or manually inserted: it copies only pointer-shaped fields, caps all
    strings/counts, dedupes repeated refs globally, and never includes bodies,
    logs, or arbitrary nested context.
    """
    requests: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str]] = set()
    seen_packages: set[str] = set()
    total_refs = 0

    for message in sorted(
        (
            m
            for m in messages
            if getattr(m, "from_type", None) in _OPERATOR_CONTEXT_FROM_TYPES
        ),
        key=_operator_context_created_sort_value,
        reverse=True,
    ):
        context = message.context if isinstance(message.context, dict) else {}
        snapshot = context.get("strategic_session")
        if not isinstance(snapshot, dict):
            continue

        descriptors = _operator_context_descriptor_map(snapshot)
        selected_refs: list[dict[str, Any]] = []
        raw_refs = snapshot.get("selected_refs")
        if isinstance(raw_refs, list):
            for raw_ref in raw_refs[:_OPERATOR_CONTEXT_REF_SCAN_LIMIT]:
                if total_refs >= _OPERATOR_CONTEXT_REFS_KEEP:
                    break
                ref = _operator_context_ref(raw_ref)
                if not ref:
                    continue
                key = (ref["kind"], ref["id"])
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                descriptor = descriptors.get(key)
                if descriptor:
                    ref = {**ref, "descriptor": descriptor}
                selected_refs.append(ref)
                total_refs += 1

        entry: dict[str, Any] = {
            "source_message_id": str(message.id),
            "created_at": message.created_at.isoformat()
            if message.created_at
            else None,
            "selected_refs": selected_refs,
        }
        for source_key, output_key, max_chars in (
            ("context_package_id", "context_package_id", 180),
            ("session_id", "session_id", 180),
            ("id", "session_id", 180),
            ("session_started_at", "session_started_at", 80),
            ("captured_at", "captured_at", 80),
        ):
            if output_key in entry:
                continue
            value = _operator_context_text(
                snapshot.get(source_key),
                max_chars=max_chars,
                raw_max_chars=max_chars * 4,
            )
            if value:
                entry[output_key] = value

        package_descriptor = _operator_context_package_descriptor(
            snapshot.get("context_package_descriptor") or snapshot.get("context_package")
        )
        if package_descriptor:
            entry["context_package_descriptor"] = package_descriptor

        package_id = entry.get("context_package_id")
        has_new_package = isinstance(package_id, str) and package_id not in seen_packages
        if isinstance(package_id, str):
            seen_packages.add(package_id)

        if selected_refs or has_new_package or package_descriptor:
            requests.append(entry)
        if len(requests) >= _OPERATOR_CONTEXT_REQUESTS_KEEP:
            break

    return requests


async def _fetch_operator_context_refs(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    result = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.from_type.in_(_OPERATOR_CONTEXT_FROM_TYPES),
        )
        .order_by(ProjectMessage.created_at.desc(), ProjectMessage.id.desc())
        .limit(_OPERATOR_CONTEXT_MESSAGE_FETCH_LIMIT)
    )
    return _build_operator_context_refs_from_messages(result.scalars().all())


def _open_message_citation_summary(context: Any) -> dict:
    """Promote a message's stored citation-grounding signal to a compact summary.

    BUG_REPORT/BLOCKER ingestion stores a reproduce-on-HEAD citation signal under
    ``context['_provenance_warnings']`` (the same key research findings use). This
    surfaces it at the top level of the packed open-message dict so the strategist
    sees the report's grounding state instead of trusting it blindly.

    The signal is surfaced in two cases:

    * ``citation_state == "absent"`` — the report is fully stale (cited code gone
      on HEAD); the strategist must discount it.
    * any state that still carries ``broken_refs``/``ungrounded_refs`` — the
      report is only PARTIALLY grounded (mixed/indeterminate). The broken and
      ungrounded references stay VISIBLE so the strategist can inspect them
      during triage instead of having that detail stripped, while the non-absent
      state keeps the report actionable (not discounted as stale).

    Returns an empty dict (no key added) when there is no recorded warning, or
    when a non-absent state carries no broken/ungrounded refs (nothing to flag).
    """
    if not isinstance(context, dict):
        return {}
    warnings = context.get("_provenance_warnings")
    if not isinstance(warnings, dict):
        return {}
    state = warnings.get("citation_state")
    broken = warnings.get("broken_refs")
    broken_list = broken if isinstance(broken, list) and broken else []
    ungrounded = warnings.get("ungrounded_refs")
    ungrounded_list = ungrounded if isinstance(ungrounded, list) and ungrounded else []
    # Surface when fully absent (stale) OR when a non-absent state still carries
    # broken/ungrounded refs (mixed/partially grounded). A non-absent state with
    # no refs has nothing to flag, so it is omitted (keeps the feed clean).
    if state != "absent" and not broken_list and not ungrounded_list:
        return {}
    summary: dict = {}
    if isinstance(state, str) and state:
        summary["citation_state"] = state
    if broken_list:
        summary["broken_refs"] = broken_list
    if ungrounded_list:
        summary["ungrounded_refs"] = ungrounded_list
    return summary


async def _fetch_open_messages(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = _OPEN_MESSAGES_KEEP,
) -> list[dict]:
    """Authoritative open-message feed for a strategic spawn payload.

    Queries messages whose status is open DIRECTLY (backed by
    ``ix_projectmessage_project_status``), independent of the capped
    conversation_thread, so an OPEN BUG_REPORT/BLOCKER stays visible to the
    strategist until it is fixed or explicitly resolved. The DB fetch orders
    critical types (BUG_REPORT/BLOCKER) FIRST within its
    ``_OPEN_MESSAGES_FETCH_LIMIT`` row window, so a freshly-filed critical is
    still fetched even when a large open backlog (>200 rows) would otherwise push
    it past an oldest-first window and out of sight entirely. The result is then
    size-bounded: at most *limit* messages, priority-ordered so BUG_REPORT/BLOCKER
    and oldest-open items survive, with per-message excerpts capped, and finally
    returned in oldest-open chronological order for a stable read.

    Note: the ``open_message_count`` scalar callers derive from this feed reflects
    the packed/bounded feed depth, not a full COUNT(*) (see
    ``payload_schemas.StrategicReviewPayload.open_message_count`` — it may
    under-report past the window but is always >= what packing surfaces).

    Shared by every strategic spawn path (strategic_review + design_doc_update)
    so the open-message access pattern stops drifting apart. Each row carries
    its full ``id`` (UUID string) so the agent can resolve it by reference.
    """
    # status == "open" is the ONLY actionable status. Messages in "linked"
    # state (bug-class messages whose fix is in-flight, owned by a direction
    # or task) are intentionally excluded here — they already have an owner
    # and the strategist should not act on them again. Messages with status
    # "resolved" are terminal and also excluded. This filter is sufficient:
    # no extra clause is needed to exclude linked messages.
    # Critical types (BUG_REPORT/BLOCKER) sort first so they are always inside the
    # fetch window regardless of age — a newly-filed critical past a >200-row open
    # backlog would otherwise never be fetched under a pure oldest-first window.
    _critical_first = case(
        (ProjectMessage.message_type.in_(_CRITICAL_OPEN_MESSAGE_DB_TYPES), 0),
        else_=1,
    )
    result = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.status == "open",
        )
        .order_by(_critical_first, ProjectMessage.created_at.asc())
        .limit(_OPEN_MESSAGES_FETCH_LIMIT)
    )
    messages = [
        {
            **agent_thread_message_dict(m),
            # Promote the reproduce-on-HEAD citation signal (stored on the
            # message context at ingestion) to the top level so a stale or
            # broken-citation bug report is VISIBLY discounted to the strategist
            # rather than silently trusted. Absent when no warning was recorded.
            **_open_message_citation_summary(m.context),
        }
        for m in result.scalars().all()
    ]
    # The DB fetch ordered criticals first (window-inclusion guarantee above);
    # re-sort into oldest-open chronological order here so the packed feed still
    # reads as a stable chronological inbox — critical-priority SURVIVAL under the
    # size bound is handled independently by _bound_open_messages.
    messages.sort(key=lambda msg: msg.get("created_at") or "")
    return _bound_open_messages(messages, limit)


# ── Recently-answered operator conversation tail ──────────────────────────────
# A bounded, explicitly NON-ACTIONABLE tail of the just-answered operator
# conversation. Once an inbound operator message is marked ``handled`` (or the
# strategist ``resolve``s it) it drops out of the open-message feed entirely and
# survives NOWHERE the restarted/compacted session can see it — so a correction
# the operator made that the strategist forgot to formally capture into the
# durable decisions register (the OPERATOR-DECISION marker) is lost forever
# across a restart, and the strategist re-proposes what the operator already
# settled. This tail restores just enough of that recent conversation for the
# model to notice a settled fact it never marked. It is read-only context, NOT
# an inbox: these messages are already answered and MUST NOT be re-acted on.
_RECENT_HANDLED_FETCH_LIMIT = 30  # DB window over recently-answered rows
_RECENT_HANDLED_KEEP = 8  # count bound for the packed tail
_RECENT_HANDLED_EXCERPT_MAX_CHARS = 600  # per-message content excerpt cap
# Terminal statuses that mean "the operator conversation was answered/closed".
# ``open`` is deliberately excluded — those already ship in _fetch_open_messages.
_RECENT_HANDLED_STATUSES = ("handled", "resolved")
# Conversational participants only. ``system`` is excluded so internal
# bookkeeping notices never leak into the operator-conversation tail.
_RECENT_HANDLED_FROM_TYPES = ("user", "operator", "strategic", "agent")


async def _fetch_recent_handled_conversation(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = _RECENT_HANDLED_KEEP,
) -> list[dict]:
    """Bounded, non-actionable tail of the recently-ANSWERED operator conversation.

    Complements ``_fetch_open_messages``: that feed carries status=="open"
    (actionable) messages; this one carries the just-answered ones
    (status in ``_RECENT_HANDLED_STATUSES``) that would otherwise vanish the
    moment they are marked handled. Restricted to conversational
    ``from_type``s (``system`` bookkeeping is excluded) so a marker-less
    operator correction survives a restart/compaction and the strategist stops
    re-proposing what the operator already settled.

    Fetches the newest ``_RECENT_HANDLED_FETCH_LIMIT`` answered rows, keeps the
    newest *limit*, caps each content excerpt, and returns them in oldest-first
    chronological order for a stable read. Purely bounded (hard count + excerpt
    cap) so it can never bloat the payload.
    """
    result = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.status.in_(_RECENT_HANDLED_STATUSES),
            ProjectMessage.from_type.in_(_RECENT_HANDLED_FROM_TYPES),
        )
        .order_by(ProjectMessage.created_at.desc())
        .limit(_RECENT_HANDLED_FETCH_LIMIT)
    )
    # Keep the newest *limit* rows (the query is newest-first), project them, then
    # re-sort into oldest-first chronological order for a stable transcript read —
    # sorting on the projected string ``created_at`` (mirrors _fetch_open_messages)
    # so no timezone-naive datetime sentinel is needed for null-safety.
    rows = list(result.scalars().all())[: max(0, limit)]
    messages: list[dict] = []
    for m in rows:
        entry = agent_thread_message_dict(m)
        content = entry.get("content")
        if isinstance(content, str) and len(content) > _RECENT_HANDLED_EXCERPT_MAX_CHARS:
            entry = {**entry, "content": content[:_RECENT_HANDLED_EXCERPT_MAX_CHARS]}
        messages.append(entry)
    messages.sort(key=lambda msg: msg.get("created_at") or "")
    return messages


def _summarize_recent_interventions(v: Any) -> dict:
    """Keep rolling rates and per-direction deltas; truncate the raw merge list."""
    if not isinstance(v, dict):
        return v
    out = {k: val for k, val in v.items() if k != "merges"}
    merges = v.get("merges")
    if isinstance(merges, list):
        out["merges"] = merges[:5]
        out["merges_total"] = len(merges)
    return out


def _summarize_capability_trial_verdicts(v: Any) -> dict:
    """Under budget pressure, prefer decided verdicts (adopt/reject) over in-trial ones."""
    if not isinstance(v, dict):
        return v
    decided = {k: e for k, e in v.items() if isinstance(e, dict) and e.get("verdict") not in (None, "continue_trial")}
    return decided if decided else dict(list(v.items())[:8])


# ── Long-tail section summarizers (budget-degradation fallbacks) ──────────────
# These six sections were previously ``exempt`` (dropped WHOLESALE under byte
# pressure). Each summarize_fn below degrades its section to a smaller-but-useful
# form — fewer entries, tighter prose, verbose sub-bodies dropped — while keeping
# the identifying keys/pointers so full detail stays reachable on demand via the
# existing fetch verbs (``./fetch playbook|knowledge|direction``). Contract
# (shared with every other summarize_fn the packer calls): PURE — build fresh
# containers, never mutate the input; NEVER raise (a raising summarize is treated
# as a drop); NEVER grow the serialized size; same top-level container type.

_OPERATOR_OVERRIDES_SUMMARIZE_KEEP = 4
_OVERRIDE_VALUE_SUMMARIZE_CAP = 80


def _summarize_operator_overrides(v: Any) -> Any:
    """Keep the newest few override entries; cut the before/after prose values.

    Preserves the load-bearing routing keys (``document``, ``field``, ``kind``,
    ``at``) so the agent still knows what it overrode where; the verbose
    ``agent_value`` / ``operator_value`` bodies are truncated hard since the full
    winning value is reconstructable from the live documents.
    """
    if not isinstance(v, list):
        return v
    out: list = []
    for e in v[:_OPERATOR_OVERRIDES_SUMMARIZE_KEEP]:
        if not isinstance(e, dict):
            out.append(e)
            continue
        out.append(
            {
                "document": e.get("document"),
                "field": e.get("field"),
                "kind": e.get("kind"),
                "agent_value": _bounded_text(
                    e.get("agent_value"), _OVERRIDE_VALUE_SUMMARIZE_CAP
                ),
                "operator_value": _bounded_text(
                    e.get("operator_value"), _OVERRIDE_VALUE_SUMMARIZE_CAP
                ),
                "at": e.get("at"),
            }
        )
    return out


_KNOWLEDGE_INDEX_SUMMARIZE_KEEP = 8
_KNOWLEDGE_INDEX_SUMMARIZE_SUMMARY_CAP = 120
# Verbose / heavy optional sub-fields dropped from each knowledge entry under
# budget pressure. The id/title/topic/direction pointers survive so the full
# body stays fetchable via ``./fetch knowledge <id>``.
_KNOWLEDGE_INDEX_HEAVY_FIELDS = (
    "per_arm_verdicts",
    "origin_provenance",
    "knowledge_acceptance_evidence",
    "compute_acceptance_reason",
    "analysis_fallback_reason",
    "criterion",
)
_KNOWLEDGE_INDEX_KEEP_KEYS = (
    "id",
    "title",
    "topic",
    "direction_id",
    "source_kind",
    "sweep_id",
    "arm_label",
    "criterion_met",
    "compute_acceptance_grade",
    "created_at",
)


def _summarize_knowledge_index(v: Any) -> Any:
    """Keep the newest few knowledge entries; drop heavy sub-fields, trim summary.

    Retains the navigation pointers (``id``, ``title``, ``topic``,
    ``direction_id``, ``source_kind`` …) so the strategist can still fetch full
    bodies via ``./fetch knowledge <id>``, while dropping the verbose per-arm
    verdicts / provenance / acceptance-reason blobs and shortening the summary.
    """
    if not isinstance(v, list):
        return v
    out: list = []
    for e in v[:_KNOWLEDGE_INDEX_SUMMARIZE_KEEP]:
        if not isinstance(e, dict):
            out.append(e)
            continue
        entry: dict[str, Any] = {
            k: e[k] for k in _KNOWLEDGE_INDEX_KEEP_KEYS if k in e
        }
        summary = _bounded_text(
            e.get("summary"), _KNOWLEDGE_INDEX_SUMMARIZE_SUMMARY_CAP
        )
        if summary is not None:
            entry["summary"] = summary
        out.append(entry)
    return out


_PREV_OUTCOMES_ROLE_KEEP = 6
_PREV_OUTCOMES_TEXT_CAP = 100
_PREV_OUTCOMES_SUMMARY_TEXT_CAP = 300
_PREV_OUTCOMES_RATIONALE_CAP = 160
# Heavy per-direction sub-fields dropped under budget pressure; the direction_id
# pointer + headline metrics survive so the outcome is still legible and the full
# direction is fetchable via ``./fetch direction <id>``.
_PREV_OUTCOMES_HEAVY_FIELDS = ("prediction", "final_metrics", "research_context")


def _summarize_previous_decision_outcome_entry(e: Any) -> Any:
    if not isinstance(e, dict):
        return e
    out = {k: val for k, val in e.items() if k not in _PREV_OUTCOMES_HEAVY_FIELDS}
    if "text" in out:
        out["text"] = _bounded_text(out.get("text"), _PREV_OUTCOMES_TEXT_CAP)
    if "rationale" in out:
        out["rationale"] = _bounded_text(
            out.get("rationale"), _PREV_OUTCOMES_RATIONALE_CAP
        )
    return out


def _summarize_previous_decision_outcomes(v: Any) -> Any:
    """Trim the prior cycle's decision-outcome digest to headline metrics.

    Keeps ``cycle_id`` / ``phase2_completed_at`` and a bounded ``created`` /
    ``retired`` list (newest few), dropping the verbose ``prediction`` /
    ``final_metrics`` sub-bodies and long prose while preserving each
    ``direction_id`` so the direction remains fetchable on demand.
    """
    if not isinstance(v, dict):
        return v
    out: dict[str, Any] = dict(v)
    if "summary_text" in out:
        out["summary_text"] = _bounded_text(
            out.get("summary_text"), _PREV_OUTCOMES_SUMMARY_TEXT_CAP
        )
    # cycle_reasoning is the single verbose free-text field — drop it entirely
    # under pressure (the compact summary_text carries the headline).
    out.pop("cycle_reasoning", None)
    for role in ("created", "retired"):
        entries = out.get(role)
        if isinstance(entries, list):
            out[role] = [
                _summarize_previous_decision_outcome_entry(e)
                for e in entries[:_PREV_OUTCOMES_ROLE_KEEP]
            ]
    return out


_PLAYBOOK_SUMMARY_ENTRIES_KEEP = 6
_PLAYBOOK_SUMMARY_CONTENT_CAP = 160
# Optional stat fields dropped from each playbook entry under budget pressure —
# the key/content headline survives and the full entry is fetchable via
# ``./fetch playbook <key>``.
_PLAYBOOK_SUMMARY_DROP_FIELDS = (
    "hit_count",
    "confirmed_count",
    "scope",
    "learned_from_direction",
    "rejection_count",
    "source",
)


def _summarize_playbook_summary(v: Any) -> Any:
    """Keep the newest few playbook entries; trim content, drop stat fields.

    Preserves each entry ``key`` (the fetch pointer) and a shortened ``content``
    headline so the strategist knows the lesson exists and can pull the full
    entry via ``./fetch playbook <key>``.
    """
    if not isinstance(v, dict):
        return v
    entries = v.get("entries")
    if not isinstance(entries, list):
        return v
    trimmed: list = []
    for e in entries[:_PLAYBOOK_SUMMARY_ENTRIES_KEEP]:
        if not isinstance(e, dict):
            trimmed.append(e)
            continue
        item: dict[str, Any] = {"key": e.get("key")}
        content = _bounded_text(e.get("content"), _PLAYBOOK_SUMMARY_CONTENT_CAP)
        if content is not None:
            item["content"] = content
        if e.get("updated_at") is not None:
            item["updated_at"] = e.get("updated_at")
        trimmed.append(item)
    out = dict(v)
    out["entries"] = trimmed
    return out


_PAST_CRITIQUES_KEEP = 3
_PAST_CRITIQUE_TEXT_CAP = 200
_PAST_CRITIQUE_TEXT_FIELDS = (
    "what_it_addressed",
    "pattern_that_produced_it",
    "lesson",
)


def _summarize_past_retirement_critiques(v: Any) -> Any:
    """Keep the newest few retirement critiques; truncate the prose fields.

    Preserves ``direction_id`` and the ``no_eligible_retirements`` sentinel;
    truncates the free-text lesson fields so the digest degrades to headlines
    rather than dropping wholesale.
    """
    if not isinstance(v, list):
        return v
    out: list = []
    for e in v[:_PAST_CRITIQUES_KEEP]:
        if not isinstance(e, dict):
            out.append(e)
            continue
        if e.get("no_eligible_retirements"):
            out.append({"no_eligible_retirements": True})
            continue
        item = dict(e)
        for field in _PAST_CRITIQUE_TEXT_FIELDS:
            if field in item:
                item[field] = _bounded_text(item.get(field), _PAST_CRITIQUE_TEXT_CAP)
        out.append(item)
    return out


_SELF_REFLECTION_KEEP = 3
_SELF_REFLECTION_TEXT_CAP = 200
_SELF_REFLECTION_TEXT_FIELDS = (
    "what_was_hard",
    "missing_context",
    "proposed_self_improvement",
)


def _summarize_self_reflection_log(v: Any) -> Any:
    """Keep the newest few self-reflection entries; truncate the prose fields.

    Preserves the ``ts`` / ``cycle_task_id`` pointers and shortens the free-text
    reflection fields so the log degrades to recent headlines under pressure.
    """
    if not isinstance(v, list):
        return v
    out: list = []
    for e in v[-_SELF_REFLECTION_KEEP:]:
        if not isinstance(e, dict):
            out.append(e)
            continue
        item = dict(e)
        for field in _SELF_REFLECTION_TEXT_FIELDS:
            if field in item:
                item[field] = _bounded_text(item.get(field), _SELF_REFLECTION_TEXT_CAP)
        out.append(item)
    return out


_SELF_REFLECTION_MIN_OCCURRENCES = 3
"""Minimum distinct entries a word must appear in to qualify as a theme."""

_SELF_REFLECTION_THEME_STOPWORDS: frozenset[str] = frozenset({
    "about", "after", "all", "also", "among", "are", "around", "because",
    "before", "been", "being", "but", "can", "could", "did", "does", "done",
    "each", "even", "everything", "from", "get", "gets", "getting", "got",
    "has", "have", "how", "into", "its", "just", "let", "like", "made",
    "make", "making", "may", "might", "more", "much", "need", "needs",
    "not", "over", "see", "seen", "should", "some", "something", "still",
    "such", "take", "than", "that", "their", "them", "thing", "things",
    "the", "this", "too", "use", "used", "using", "very", "way", "ways",
    "well", "were", "what", "when", "where", "which", "while", "who",
    "will", "with", "without", "within", "would",
})


def _build_self_reflection_themes(log: list | None) -> list[dict]:
    """Extract recurring themes from the full self-reflection log.

    When the log has more than 5 entries, words appearing in at least
    ``_SELF_REFLECTION_MIN_OCCURRENCES`` distinct entries' ``what_was_hard``
    are surfaced as compact theme entries — one per word with an occurrence
    count — so the strategist agent can spot patterns the recent-only log
    summary would hide.

    Returns up to 5 theme dicts sorted by descending occurrence count, or an
    empty list when the log is too short or no word meets the threshold.
    """
    if not isinstance(log, list) or len(log) <= 5:
        return []
    word_entry_count: dict[str, int] = {}
    for entry in log:
        if not isinstance(entry, dict):
            continue
        text = (entry.get("what_was_hard") or "").lower()
        words = set()
        for m in re.finditer(r"[a-z]{3,}", text):
            w = m.group()
            if w not in _SELF_REFLECTION_THEME_STOPWORDS:
                words.add(w)
        for w in words:
            word_entry_count[w] = word_entry_count.get(w, 0) + 1
    themes = [
        {"theme": w, "occurrences": c}
        for w, c in word_entry_count.items()
        if c >= _SELF_REFLECTION_MIN_OCCURRENCES
    ]
    themes.sort(key=lambda x: (-x["occurrences"], x["theme"]))
    return themes[:5]
