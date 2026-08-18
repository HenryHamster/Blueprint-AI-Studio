"""Pure-function helpers for claim_task endpoint."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Helper: cycle detection for spawn dependency graphs
# ---------------------------------------------------------------------------


def _has_local_cycle(adj: Dict[str, list]) -> bool:
    """Check for cycles in a small adjacency list (dependency graph)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adj, WHITE)

    def dfs(n):
        color[n] = GRAY
        for dep in adj.get(n, []):
            if dep not in color:
                continue  # External dep, not in our subgraph
            if color[dep] == GRAY:
                return True
            if color[dep] == WHITE and dfs(dep):
                return True
        color[n] = BLACK
        return False

    return any(color[n] == WHITE and dfs(n) for n in adj)


# ---------------------------------------------------------------------------
# Helper: parse a Failed Approaches entry into {text, failure_type}
# ---------------------------------------------------------------------------

_KNOWN_FAILURE_CATEGORIES = frozenset(
    {
        "CODE_ERROR",
        "PROVISION_FAILURE",
        "AGENT_TIMEOUT",
        "EXTERNAL_API",
        "RATE_LIMIT",
        "DEAD_AUTH",
        "rejected in review",
    }
)


def _parse_failed_approach(line: str) -> dict:
    """Parse '<title> (<category>) [at=<iso> stage=<stage>] — <reason>'.

    Returns {text, failure_type, failed_at, failure_stage}. Direction outcomes
    write entries with the failure category in parentheses immediately after the
    title, optionally followed by a ``[at=YYYY-MM-DD stage=<stage>]`` metadata
    block. Extract those so agents can reason about *why* a prior approach
    failed, *when* it failed, and *at what stage*. Only recognise parens whose
    contents match a known category so prose-level parens don't get treated as
    metadata.
    """
    failure_type = ""
    failed_at = ""
    failure_stage = ""
    text = line
    open_idx = line.find("(")
    if open_idx > 0:
        close_idx = line.find(")", open_idx)
        if close_idx > open_idx:
            inner = line[open_idx + 1 : close_idx].strip()
            if inner in _KNOWN_FAILURE_CATEGORIES:
                failure_type = inner
                rest = line[close_idx + 1 :]
                # Look for a [at=... stage=...] metadata block that follows the
                # category paren with only whitespace in between.
                meta_start = -1
                for i, ch in enumerate(rest):
                    if ch == "[":
                        meta_start = i
                        break
                    if not ch.isspace():
                        break
                if meta_start >= 0:
                    meta_end = rest.find("]", meta_start)
                    if meta_end > meta_start:
                        meta = rest[meta_start + 1 : meta_end]
                        for part in meta.split():
                            if part.startswith("at="):
                                failed_at = part[len("at=") :]
                            elif part.startswith("stage="):
                                failure_stage = part[len("stage=") :]
                        rest = rest[:meta_start].rstrip() + rest[meta_end + 1 :]
                stripped = (line[:open_idx].rstrip() + rest).strip()
                text = stripped if stripped else line
    return {
        "text": text[:200],
        "failure_type": failure_type,
        "failed_at": failed_at,
        "failure_stage": failure_stage,
    }


# ---------------------------------------------------------------------------
# Helper: failure stage pattern analysis
# ---------------------------------------------------------------------------


def _compute_failure_stage_pattern(history: list) -> "dict | None":
    """
    Analyze recent research_history stage_dropout data to identify dominant failure stages.

    Returns a pattern dict if 60%+ of non-infra failures cluster at one stage, else None.
    Considers code_error, approval, and staging_only stages — agents can't address infra failures.
    Uses the last 3 research cycles for recency.
    """
    recent = history[-3:]
    code_error_total = 0
    approval_total = 0
    staging_only_total = 0
    for entry in recent:
        sd = entry.get("stage_dropout") or {}
        code_error_total += sd.get("tasks_failed_code_error", 0)
        approval_total += sd.get("tasks_required_approval", 0)
        staging_only_total += sd.get("tasks_merged_staging_only", 0)
    total_non_infra = code_error_total + approval_total + staging_only_total
    if total_non_infra < 3:
        return None
    code_error_rate = code_error_total / total_non_infra
    approval_rate = approval_total / total_non_infra
    staging_only_rate = staging_only_total / total_non_infra
    threshold = 0.6
    if code_error_rate >= threshold:
        return {
            "stage": "code_error",
            "rate": round(code_error_rate, 2),
            "message": (
                "Recent tasks for this direction frequently fail at the code/test stage — "
                "pay extra attention to test coverage and run tests early."
            ),
        }
    if approval_rate >= threshold:
        return {
            "stage": "approval",
            "rate": round(approval_rate, 2),
            "message": (
                "Recent tasks for this direction frequently stall waiting for approval — "
                "stay within the scope's file and spend budget; that's what avoids the "
                "approval gate, not the size of the change."
            ),
        }
    if staging_only_rate >= threshold:
        return {
            "stage": "staging_only",
            "rate": round(staging_only_rate, 2),
            "message": (
                "Recent tasks for this direction frequently merge to staging but fail the rollup to main — "
                "watch for integration-level issues like merge conflicts and CI failures against the target branch."
            ),
        }
    return None


# ---------------------------------------------------------------------------
# Helper: compact strategic vision for the claim response
# ---------------------------------------------------------------------------


def _build_vision_summary(vision_raw: dict | None) -> "dict | None":
    """Return a size-bounded, agent-safe view of project.vision.

    Strips internal fields (``_strategic_failures``, ``_strategic_cycle_history``,
    ``_strategic_frequency_override``, etc.) and truncates free-text entries so
    the claim payload stays bounded. Returns None when there is nothing
    meaningful to surface — so callers can skip injection entirely.
    """
    if not vision_raw or not isinstance(vision_raw, dict):
        return None
    summary: dict = {}
    purpose = vision_raw.get("purpose")
    if isinstance(purpose, str) and purpose.strip():
        summary["purpose"] = purpose.strip()[:600]
    success = vision_raw.get("success_criteria")
    if isinstance(success, str) and success.strip():
        summary["success_criteria"] = success.strip()[:600]
    elif isinstance(success, list):
        compact = [
            str(s).strip()[:200] for s in success if isinstance(s, str) and s.strip()
        ]
        if compact:
            summary["success_criteria"] = compact[:5]
    context_field = vision_raw.get("context")
    if isinstance(context_field, str) and context_field.strip():
        summary["context"] = context_field.strip()[:600]
    priorities = vision_raw.get("priorities")
    if isinstance(priorities, list):
        compact_p: list = []
        for p in priorities[:5]:
            if isinstance(p, str) and p.strip():
                compact_p.append(p.strip()[:200])
            elif isinstance(p, dict):
                text = p.get("text") or p.get("title") or p.get("name")
                if isinstance(text, str) and text.strip():
                    compact_p.append(text.strip()[:200])
        if compact_p:
            summary["priorities"] = compact_p
    systems = vision_raw.get("systems")
    if isinstance(systems, list):
        compact_s: list = []
        for sys in systems[:6]:
            if not isinstance(sys, dict):
                continue
            name = sys.get("name") or sys.get("id")
            if not isinstance(name, str) or not name.strip():
                continue
            entry = {"name": name.strip()[:80]}
            status = sys.get("status")
            if isinstance(status, str) and status.strip():
                entry["status"] = status.strip()[:40]
            desc = sys.get("description")
            if isinstance(desc, str) and desc.strip():
                entry["description"] = desc.strip()[:200]
            compact_s.append(entry)
        if compact_s:
            summary["systems"] = compact_s
    revision = vision_raw.get("revision")
    if isinstance(revision, int):
        summary["revision"] = revision
    last_updated = vision_raw.get("last_updated")
    if isinstance(last_updated, str) and last_updated:
        summary["last_updated"] = last_updated[:40]
    if not summary:
        return None
    return summary


# ---------------------------------------------------------------------------
# Helper: live problem-model intent for the claim response
# ---------------------------------------------------------------------------


def _build_problem_model_summary(pm_raw: dict | None) -> "dict | None":
    """Return a size-bounded, agent-safe view of the LIVE ``project.problem_model``.

    Surfaces the operator-editable strategic intent — ``intent.purpose``,
    ``intent.current_focus``, the ``context`` framing
    (project_type/users/team/deploy_cadence/notes), and ``constraints`` — so
    worker agents see current steering immediately, without waiting for a
    strategic cycle to mirror it into the legacy ``vision`` blob. Truncates
    free-text the same way :func:`_build_vision_summary` bounds its fields.

    Fails open: returns ``None`` on missing/malformed input or when the model is
    empty (per :func:`app.problem_model.is_empty`) so a bad problem_model never
    500s the claim and callers can fall back to the legacy vision summary.
    """
    if not pm_raw or not isinstance(pm_raw, dict):
        return None
    try:
        from app.problem_model import is_empty, parse_problem_model

        model = parse_problem_model(pm_raw)
    except Exception:
        return None
    if is_empty(model):
        return None

    summary: dict = {}
    user_purpose = model.intent.user_purpose
    if isinstance(user_purpose, str) and user_purpose.strip():
        summary["user_purpose"] = user_purpose.strip()[:600]
    purpose = model.intent.purpose
    if isinstance(purpose, str) and purpose.strip():
        summary["purpose"] = purpose.strip()[:600]
    current_focus = model.intent.current_focus
    if isinstance(current_focus, str) and current_focus.strip():
        summary["current_focus"] = current_focus.strip()[:600]

    context_summary: dict = {}
    for field in ("project_type", "users", "team", "deploy_cadence", "notes"):
        val = getattr(model.context, field, None)
        if isinstance(val, str) and val.strip():
            context_summary[field] = val.strip()[:300]
    if context_summary:
        summary["context"] = context_summary

    if isinstance(model.constraints, list):
        compact = [
            str(c).strip()[:200]
            for c in model.constraints[:8]
            if isinstance(c, str) and c.strip()
        ]
        if compact:
            summary["constraints"] = compact

    if not summary:
        return None
    return summary


# ---------------------------------------------------------------------------
# Helper: current direction contract (success evidence / completion / retirement)
# ---------------------------------------------------------------------------


def _build_direction_contract(direction: dict) -> "dict | None":
    """Extract the current direction's contract fields for the worker prompt.

    ``completion_criteria`` lives directly on the direction dict;
    ``success_evidence`` and ``retirement_condition`` live under
    ``direction["context"]`` (see ``routers/directions.py``). Bounds each
    free-text field the same way the other claim helpers do. Returns ``None``
    when no contract field is set so callers can skip injection.
    """
    if not isinstance(direction, dict):
        return None
    contract: dict = {}
    completion = direction.get("completion_criteria")
    if isinstance(completion, str) and completion.strip():
        contract["completion_criteria"] = completion.strip()[:600]
    ctx = direction.get("context")
    if isinstance(ctx, dict):
        success_evidence = ctx.get("success_evidence")
        if isinstance(success_evidence, str) and success_evidence.strip():
            contract["success_evidence"] = success_evidence.strip()[:600]
        retirement_condition = ctx.get("retirement_condition")
        if isinstance(retirement_condition, str) and retirement_condition.strip():
            contract["retirement_condition"] = retirement_condition.strip()[:600]
    if not contract:
        return None
    return contract


# ---------------------------------------------------------------------------
# Helper: direction narrative (what we know, what's open)
# ---------------------------------------------------------------------------


def _build_direction_narrative(direction: dict) -> "dict | None":
    """Extract a short "where we are" narrative from a direction.

    Pulls the top bullets from the Established Findings and Open Questions
    sections of research_context, plus the most recent productive cycle
    outcome. The aim is to give tactical agents a sense of trajectory —
    what's been confirmed, what's still unknown — without dumping the full
    research document into the prompt.
    """
    if not isinstance(direction, dict):
        return None
    from app.research_context import _parse_research_context

    rc_text = direction.get("research_context", "") or ""
    parsed = _parse_research_context(rc_text) if rc_text else {}
    established = parsed.get("established", []) if isinstance(parsed, dict) else []
    open_questions = (
        parsed.get("open_questions", []) if isinstance(parsed, dict) else []
    )

    def _trim(items: list, limit: int, char_cap: int) -> list:
        return [
            item.strip()[:char_cap]
            for item in items[:limit]
            if isinstance(item, str) and item.strip()
        ]

    narrative: dict = {}
    top_established = _trim(established, 3, 240)
    if top_established:
        narrative["established_findings"] = top_established
    top_open = _trim(open_questions, 2, 240)
    if top_open:
        narrative["open_questions"] = top_open

    history = direction.get("research_history") or []
    productive = [
        h for h in history if isinstance(h, dict) and h.get("tasks_created", 0) > 0
    ]
    if productive:
        last = productive[-1]
        merged = int(last.get("prs_merged", 0) or 0)
        created = int(last.get("tasks_created", 0) or 0)
        rejected = int(last.get("prs_rejected", 0) or 0)
        if created > 0:
            narrative["last_cycle"] = {
                "tasks_created": created,
                "prs_merged": merged,
                "prs_rejected": rejected,
            }
    if not narrative:
        return None
    return narrative


# ---------------------------------------------------------------------------
# Stale confirmed-command helpers
# ---------------------------------------------------------------------------

_STALE_THRESHOLD_DAYS = 30


def is_commands_stale(confirmed_at: str | None) -> bool:
    """Return True when confirmed_at is absent or older than STALE_THRESHOLD_DAYS."""
    if not confirmed_at:
        return True
    try:
        ts = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(tz=timezone.utc) - ts > timedelta(days=_STALE_THRESHOLD_DAYS)
    except (ValueError, TypeError):
        return True


def annotate_stale_build_config_dirs(
    build_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a copy of build_cfg with commands_stale annotated on each directory.

    Does not mutate the original dict.  The injected ``commands_stale`` field
    is purely informational for the claiming agent — it is never persisted.
    """
    if not build_cfg:
        return build_cfg
    directories = build_cfg.get("directories")
    if not directories:
        return build_cfg
    annotated_dirs: List[Dict[str, Any]] = []
    for entry in directories:
        if isinstance(entry, dict):
            annotated = dict(entry)
            annotated["commands_stale"] = is_commands_stale(
                entry.get("commands_confirmed_at")
            )
            annotated_dirs.append(annotated)
        else:
            annotated_dirs.append(entry)
    result = dict(build_cfg)
    result["directories"] = annotated_dirs
    return result
