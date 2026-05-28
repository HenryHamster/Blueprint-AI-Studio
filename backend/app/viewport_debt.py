"""Viewport opt-out debt analytics.

Extracted from ``routers/admin_digest.py`` (direction d-ce93e938 — serialize
one monolith at a time). Scans ``admin-ui/src/components/*.tsx`` for
viewport escape-hatch opt-outs and surfaces a per-component debt summary.

The route is mounted as a sub-router that ``admin_digest`` includes, so the
public path (``/api/admin/projects/{project_id}/viewport-debt``), role check,
caching, and response shape are unchanged.
"""

import asyncio
import re as _re
import uuid
from datetime import datetime, timedelta
from pathlib import Path as _Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import require_admin
from app.database import get_session
from app.models import Project, RegressionRemediationCase

router = APIRouter()

_VIEWPORT_DEBT_CACHE: Optional[
    tuple[datetime, List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]
] = None
_VIEWPORT_DEBT_CACHE_TTL = 300  # 5 minutes
_VIEWPORT_DEBT_UNTESTED_CAP = 30

# Matches any eslint-disable comment that references "viewport" — canonical pattern
# shared with the CI gate in admin-ui/src/test/viewport-checks.js
# (checkEscapeHatchBudget uses: /eslint-disable[^\n]*viewport/g).
_ESLINT_VIEWPORT_RE = _re.compile(r"eslint-disable[^\n]*viewport")
_DATA_SMALL_TARGET_RE = _re.compile(r"data-viewport-allow-small-target")
_DATA_OVERFLOW_RE = _re.compile(r"data-viewport-allow-overflow")
_DATA_TRUNCATION_RE = _re.compile(r"data-viewport-allow-truncation")

# Mirror of EXEMPT_FROM_VIEWPORT_TEST in admin-ui/src/test/viewport-checks.js.
# Components here are internal/utility and never rendered standalone on operator
# pages — keep in sync so the Viewport Debt panel "untested" count matches CI.
_EXEMPT_FROM_VIEWPORT_TEST: frozenset[str] = frozenset(
    [
        "ErrorBoundary",
        "MarkdownContent",
        "ColumnsPicker",
        "Toasts",
        "catchupRows",
        "regressionPhaseChips",
        "VersionLifecyclePanel",
        # Message-row sub-component rendered exclusively inside MessagesPanel;
        # viewport coverage is provided by MessagesPanel.viewport.test.tsx.
        "MessageBubble",
        # Needs-attention worklist sub-component rendered exclusively inside
        # CatchUpPanel; viewport coverage is provided by CatchUpPanel.viewport.test.tsx.
        "CatchUpNeedsAttentionSection",
    ]
)


def _find_components_dir() -> Optional[_Path]:
    here = _Path(__file__).resolve().parent
    for _ in range(6):
        candidate = here / "admin-ui" / "src" / "components"
        if candidate.is_dir():
            return candidate
        here = here.parent
    return None


def _load_viewport_budget() -> Dict[str, Any]:
    """Load viewport-escape-hatch-budget.json, returning empty dict on failure."""
    here = _Path(__file__).resolve().parent
    for _ in range(6):
        candidate = (
            here / "admin-ui" / "src" / "test" / "viewport-escape-hatch-budget.json"
        )
        if candidate.is_file():
            try:
                import json as _json

                return _json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                return {}
        here = here.parent
    return {}


def _scan_viewport_debt_rows() -> tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]
]:
    global _VIEWPORT_DEBT_CACHE
    now = datetime.utcnow()
    if _VIEWPORT_DEBT_CACHE is not None:
        cached_at, cached_rows, cached_untested, cached_summary = _VIEWPORT_DEBT_CACHE
        if (now - cached_at).total_seconds() < _VIEWPORT_DEBT_CACHE_TTL:
            return cached_rows, cached_untested, cached_summary

    components_dir = _find_components_dir()
    if components_dir is None:
        empty_summary: Dict[str, Any] = {
            "total_components": 0,
            "untested_count": 0,
            "untested_components": [],
            "total_opt_outs": 0,
            "total_budget": None,
            "budget_headroom": None,
        }
        return [], [], empty_summary

    budget = _load_viewport_budget()
    per_component_budget: Dict[str, int] = budget.get("perComponent", {}) or {}
    total_budget: Optional[int] = budget.get("total")

    rows = []
    untested_rows: List[Dict[str, Any]] = []
    untested_paths: List[str] = []
    total_components = 0
    grand_total_opt_outs = 0
    for tsx_file in sorted(components_dir.glob("*.tsx")):
        name = tsx_file.name
        if ".test." in name or ".spec." in name:
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        total_components += 1
        eslint_count = len(_ESLINT_VIEWPORT_RE.findall(content))
        small_target = len(_DATA_SMALL_TARGET_RE.findall(content))
        overflow = len(_DATA_OVERFLOW_RE.findall(content))
        truncation = len(_DATA_TRUNCATION_RE.findall(content))
        total = eslint_count + small_target + overflow + truncation
        grand_total_opt_outs += total

        stem = tsx_file.stem  # e.g. "MessagesPanel"
        vp_test = (tsx_file.parent / f"{stem}.viewport.test.tsx").exists()
        rel_path = f"admin-ui/src/components/{name}"

        cap: Optional[int] = per_component_budget.get(stem)
        headroom: Optional[int] = (cap - total) if cap is not None else None

        if not vp_test and stem not in _EXEMPT_FROM_VIEWPORT_TEST:
            untested_paths.append(rel_path)

        if total == 0:
            if not vp_test and stem not in _EXEMPT_FROM_VIEWPORT_TEST:
                untested_rows.append(
                    {
                        "component": rel_path,
                        "eslint_disable_count": 0,
                        "data_viewport_allow_small_target": 0,
                        "data_viewport_allow_overflow": 0,
                        "data_viewport_allow_truncation": 0,
                        "total_opt_outs": 0,
                        "budget_cap": cap,
                        "headroom": headroom,
                        "has_viewport_test": False,
                        "regression_case_count": 0,
                        "regression_case_breakdown": {},
                        "source_url": None,
                    }
                )
            continue

        rows.append(
            {
                "component": rel_path,
                "eslint_disable_count": eslint_count,
                "data_viewport_allow_small_target": small_target,
                "data_viewport_allow_overflow": overflow,
                "data_viewport_allow_truncation": truncation,
                "total_opt_outs": total,
                "budget_cap": cap,
                "headroom": headroom,
                "has_viewport_test": vp_test,
                "regression_case_count": 0,
                "regression_case_breakdown": {},
                "source_url": None,
            }
        )

    rows.sort(key=lambda r: r["total_opt_outs"], reverse=True)
    untested_rows.sort(key=lambda r: r["component"])

    budget_headroom: Optional[int] = (
        (total_budget - grand_total_opt_outs) if total_budget is not None else None
    )
    coverage_summary: Dict[str, Any] = {
        "total_components": total_components,
        "untested_count": len(untested_paths),
        "untested_components": sorted(untested_paths)[:50],
        "total_opt_outs": grand_total_opt_outs,
        "total_budget": total_budget,
        "budget_headroom": budget_headroom,
    }

    rows = rows[:15]
    untested_rows = untested_rows[:_VIEWPORT_DEBT_UNTESTED_CAP]
    _VIEWPORT_DEBT_CACHE = (now, rows, untested_rows, coverage_summary)
    return rows, untested_rows, coverage_summary


@router.get("/projects/{project_id}/viewport-debt")
async def get_viewport_debt(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Per-component viewport opt-out debt summary.

    Scans admin-ui/src/components/*.tsx for eslint-disable viewport/* lines
    and data-viewport-allow-* attributes.  Returns top-15 components sorted
    by total opt-out count, with a flag indicating whether a .viewport.test.tsx
    companion exists and a count of viewport_regression dispatcher cases in the
    last 30 days.  Result is cached in-process for 5 minutes.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

    # Shallow-copy cached rows: the scan result is a process-wide cache and
    # we annotate per-project below. Without the copy, concurrent requests
    # for different projects would race on regression_case_count / source_url.
    top_rows, top_untested, coverage_summary = await asyncio.to_thread(
        _scan_viewport_debt_rows
    )
    rows = [dict(r) for r in top_rows]
    untested_rows = [dict(r) for r in top_untested]

    if rows or untested_rows:
        from app.regression_layer_classifier import (  # noqa: PLC0415
            parse_viewport_regression_class,
        )

        cutoff = datetime.utcnow() - timedelta(days=30)
        stmt = select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.regression_class.startswith(
                "viewport_regression:"
            ),
            RegressionRemediationCase.first_seen_at >= cutoff,
        )
        vp_cases = (await session.execute(stmt)).scalars().all()

        # Map component stem → case count and per-assertion-type breakdown
        case_counts: Dict[str, int] = {}
        case_breakdown: Dict[str, Dict[str, int]] = {}
        for c in vp_cases:
            parsed = parse_viewport_regression_class(c.regression_class)
            comp = parsed.get("component")
            if comp:
                # Normalize component name to .tsx extension for consistent lookup
                if not comp.endswith(".tsx"):
                    comp = f"{comp}.tsx"
                case_counts[comp] = case_counts.get(comp, 0) + 1
                atype = parsed.get("assertion_type") or "unknown"
                bd = case_breakdown.setdefault(comp, {})
                bd[atype] = bd.get(atype, 0) + 1

        project = await session.get(Project, project_id)
        gh_owner = project.github_owner if project else None
        gh_repo = project.github_repo if project else None

        for row in (*rows, *untested_rows):
            stem_tsx = _Path(row["component"]).name  # e.g. "MessagesPanel.tsx"
            row["regression_case_count"] = case_counts.get(stem_tsx, 0)
            row["regression_case_breakdown"] = case_breakdown.get(stem_tsx, {})
            if gh_owner and gh_repo:
                row["source_url"] = (
                    f"https://github.com/{gh_owner}/{gh_repo}/blob/main/{row['component']}"
                )

    return {
        "rows": rows,
        "untested_rows": untested_rows,
        "scanned_at": datetime.utcnow().isoformat(),
        "coverage_summary": coverage_summary,
    }
