"""First-attempt success pattern capture and recent-success summaries.

Extracted from ``playbook_learning.py`` so success-pattern behavior lives next
to its tests instead of inside the broader playbook-learning monolith.
``app.playbook_learning`` re-exports the public symbols so existing callers and
tests that patch via ``app.playbook_learning.<name>`` keep working unchanged.
"""

import json
import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Project,
    Task,
    TaskStatus,
    _utcnow,
)
from app.playbook_storage import maybe_archive_for_capacity
from app.task_capabilities import ALL_TASK_TYPES

logger = logging.getLogger(__name__)


# Task types where surfacing 'recent successes' patterns at spawn time is useful.
# Other types (review, plan, qa, audit, etc.) treat this signal as noise.
RECENT_SUCCESSES_TASK_TYPES: frozenset[str] = frozenset({"create", "fix", "research"})


def _purge_retired_success_entries(doc: dict) -> int:
    """Remove playbook/success-* entries whose task type is no longer in the live registry.

    Entries are keyed as ``playbook/success-{task_type}-{scope_slug}``.  When a
    task type is retired (removed from ``ALL_TASK_TYPES``), its entries become
    orphans that would otherwise persist forever.  Returns the count removed.
    """
    prefix = "playbook/success-"
    to_remove = []
    for key in list(doc):
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        # The first "-" separates task_type from scope_slug.
        hyphen_idx = rest.find("-")
        task_type_part = rest[:hyphen_idx] if hyphen_idx >= 0 else rest
        if task_type_part not in ALL_TASK_TYPES:
            to_remove.append(key)
    for key in to_remove:
        del doc[key]
    return len(to_remove)


async def capture_success_pattern(
    task: Task,
    session: AsyncSession,
) -> Optional[str]:
    """Capture a positive pattern from a first-attempt success as a playbook entry.

    Uses a total-completions noise filter: only captures a success pattern after the
    task type has accumulated at least 3 total completions (successful or failed) in the
    project.  This avoids noise from one-off tasks without requiring failures first,
    solving the cold-start problem for new task types like 'migrate' or 'perf'.

    One entry per (task_type, scope) pair — e.g. ``playbook/success-create-small``.
    Each entry tracks success_count, scope_distribution (how many successes per scope
    level), and lightweight metadata: num_files_changed, tests_added, first_attempt_success.

    Returns the short key (e.g. ``"success-create-small"``) if an entry was created or
    updated, ``None`` if the noise filter was not met.
    """
    task_type = task.first_payload.get("type", "")
    if not task_type:
        return None

    if task_type not in ALL_TASK_TYPES:
        logger.debug(
            "[SUCCESS_PLAYBOOK] project=%s type=%s: not in live task-type registry — skipping",
            task.project_id,
            task_type,
        )
        return None

    scope: str = (task.context or {}).get("scope") or "unspecified"
    related_files: list[str] = list((task.context or {}).get("related_files") or [])
    direction_id: str = (task.context or {}).get("direction_id") or ""
    num_files = len(related_files)

    # Richer implementation-shape signals: file-type distribution and test count.
    file_type_dist: dict[str, int] = {}
    test_file_count = 0
    for f in related_files:
        if (
            "/test_" in f
            or f.startswith("test_")
            or ".test." in f
            or ".spec." in f
            or "/testing/" in f
            or "/tests/" in f
        ):
            test_file_count += 1
        ext = f.rsplit(".", 1)[-1] if "." in f else ""
        if ext and len(ext) <= 6:
            file_type_dist[ext] = file_type_dist.get(ext, 0) + 1

    tests_added = test_file_count > 0

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == task.project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None

    # Noise filter: require at least 3 total completions (success or failure) for
    # this task type before capturing a success pattern.  Use a COUNT query with
    # JSONB type filter to avoid loading all project tasks into memory.
    prior_count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == task.project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                Task.id != task.id,
                Task.payloads[0]["type"].as_string() == task_type,
            )
        )
    ).scalar_one()
    same_type_count = prior_count + 1  # +1 for the current task

    if same_type_count < 3:
        logger.debug(
            "[SUCCESS_PLAYBOOK] project=%s type=%s: only %d completion(s) — noise filter not met",
            task.project_id,
            task_type,
            same_type_count,
        )
        return None

    doc = dict(project.design_doc or {})

    # One entry per (task_type, scope) pair.
    scope_slug = scope.lower().replace(" ", "-")
    key = f"success-{task_type}-{scope_slug}"
    playbook_full_key = f"playbook/{key}"
    files_note = f" Files: {', '.join(related_files[:5])}." if related_files else ""

    existing = doc.get(playbook_full_key)
    if existing and isinstance(existing, dict):
        entry = dict(existing)
        count = entry.get("success_count", 1) + 1
        scope_dist = dict(entry.get("scope_distribution") or {})
        scope_dist[scope] = scope_dist.get(scope, 0) + 1
        raw_dir_succ = entry.get("direction_successes")
        dir_succ = dict(raw_dir_succ) if isinstance(raw_dir_succ, dict) else {}
        if direction_id:
            dir_succ[direction_id] = dir_succ.get(direction_id, 0) + 1
        entry["success_count"] = count
        entry["scope_distribution"] = scope_dist
        entry["direction_successes"] = dir_succ
        entry["num_files_changed"] = num_files
        entry["tests_added"] = tests_added
        entry["test_file_count"] = test_file_count
        entry["file_type_distribution"] = file_type_dist
        entry["updated_at"] = _utcnow().isoformat()
        entry["content"] = (
            f"First-attempt success pattern for {task_type} tasks "
            f"({count} observations, latest scope: {scope}).{files_note} "
            f"Scope distribution: {scope_dist}."
        )
        doc[playbook_full_key] = entry
        action = "updated"
    else:
        content = (
            f"First-attempt success pattern for {task_type} tasks "
            f"(1 observation, scope: {scope}).{files_note} "
            f"Files changed: {num_files}, tests added: {tests_added}."
        )
        new_entry = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "success_count": 1,
            "source": "success-pattern",
            "applicable_types": [task_type],
            "scope": scope,
            "num_files_changed": num_files,
            "tests_added": tests_added,
            "test_file_count": test_file_count,
            "file_type_distribution": file_type_dist,
            "direction_successes": {direction_id: 1} if direction_id else {},
            "first_attempt_success": True,
            "scope_distribution": {scope: 1},
        }
        doc = maybe_archive_for_capacity(doc, len(json.dumps(new_entry).encode()))
        doc[playbook_full_key] = new_entry
        action = "created"

    purged = _purge_retired_success_entries(doc)
    if purged:
        logger.info(
            "[SUCCESS_PLAYBOOK] project=%s purged %d orphan entry(ies) for retired task type(s)",
            task.project_id,
            purged,
        )

    meta = dict(doc.get("_meta", {"revision": 0}))
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by"] = "success-playbook"
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SUCCESS_PLAYBOOK] project=%s %s entry '%s' for task_type=%s scope=%s",
        task.project_id,
        action,
        key,
        task_type,
        scope,
    )
    return key


def summarize_recent_successes(
    design_doc: dict,
    task_type: str,
    direction_id: Optional[str],
    scope: Optional[str],
    *,
    max_bullets: int = 5,
    char_budget: int = 600,
) -> Optional[str]:
    """Return a markdown 'Recent successes' section drawn from playbook/success-* entries.

    Helper is task-type-agnostic — call sites should gate on
    ``RECENT_SUCCESSES_TASK_TYPES``. Reads only from *design_doc*; performs no
    I/O. Returns None when nothing qualifies so callers can omit the section.

    Ranking: direction-affinity (direction_successes[direction_id] > 0), then
    exact (task_type, scope) match, then ``success_count``, then ``updated_at``
    recency. Entries with success_count < 2 are filtered out.

    Output is hard-capped at *char_budget* by dropping trailing bullets — never
    truncated mid-line. Returns None if even the first bullet doesn't fit.
    """
    if not isinstance(design_doc, dict) or not task_type:
        return None
    prefix = f"playbook/success-{task_type}-"
    candidates: list[tuple[str, dict]] = []
    for k, v in design_doc.items():
        if not isinstance(v, dict) or not k.startswith(prefix):
            continue
        if int(v.get("success_count") or 0) < 2:
            continue
        candidates.append((k, v))
    if not candidates:
        return None

    scope_slug = (scope or "").lower().replace(" ", "-")
    exact_key = f"playbook/success-{task_type}-{scope_slug}" if scope_slug else None

    def _rank(item: tuple[str, dict]) -> tuple:
        k, v = item
        dir_succ = v.get("direction_successes") or {}
        affinity = (
            int(dir_succ.get(direction_id, 0))
            if direction_id and isinstance(dir_succ, dict)
            else 0
        )
        scope_match = 1 if exact_key and k == exact_key else 0
        return (
            affinity,
            scope_match,
            int(v.get("success_count") or 0),
            v.get("updated_at") or "",
        )

    candidates.sort(key=_rank, reverse=True)

    bullets: list[str] = []
    for k, v in candidates[:max_bullets]:
        scope_label = k[len(prefix) :] or "unspecified"
        sc = int(v.get("success_count") or 0)
        parts = [f"{task_type}-{scope_label} ({sc}x)"]
        ftd = v.get("file_type_distribution")
        if isinstance(ftd, dict) and ftd:
            top_exts = sorted(ftd.items(), key=lambda kv: -int(kv[1] or 0))[:3]
            parts.append("files: " + ", ".join(f"{ext}:{n}" for ext, n in top_exts))
        if v.get("tests_added"):
            tfc = int(v.get("test_file_count") or 0)
            parts.append(f"tests added ({tfc})" if tfc else "tests added")
        dir_succ = v.get("direction_successes")
        if direction_id and isinstance(dir_succ, dict):
            local = int(dir_succ.get(direction_id, 0))
            if local > 0:
                parts.append(f"this direction: {local}")
        bullets.append("- " + "; ".join(parts))

    if not bullets:
        return None

    header = "## Recent successes (what has compounded here)"
    out_lines: list[str] = [header]
    out_len = len(header)
    for b in bullets:
        added = len(b) + 1  # +1 for the joining newline
        if out_len + added > char_budget:
            break
        out_lines.append(b)
        out_len += added

    if len(out_lines) == 1:
        return None
    return "\n".join(out_lines)


__all__ = [
    "RECENT_SUCCESSES_TASK_TYPES",
    "_purge_retired_success_entries",
    "capture_success_pattern",
    "summarize_recent_successes",
]
