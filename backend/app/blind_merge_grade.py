"""Blind merge-grade qualification gate.

A release version must not transition ``stabilize -> qualify`` until an
*uninvested* grade of the merged diffs against the version goal has been
computed and persisted.  "Uninvested / blind" means the grade is built from
ONLY two inputs — the merged diffs of the version and ``version.goal`` — and
NEVER from the strategist's narrative (``Project.running_update``) or any other
self-reported progress signal.  A grader that can read the strategist's own
account of what was done is not a check; it is an echo.

The grade is a synchronous direct-LLM call (mirroring
``review_learning.categorize_review_issues_llm`` and
``knowledge_completion._call_compute_analysis_llm``): backend-only, gated behind
``ANTHROPIC_API_KEY``, with a JSON-parse retry and a graceful ``None`` fallback.
It is deliberately NOT a spawned agent task — keeping the gate inside the
qualifier process makes it testable with targeted backend tests and avoids
booting an agent VM purely to grade a diff.

Public API:
  * ``compute_blind_merge_grade(session, project, version, candidate_sha)`` —
    assemble the merged-diff text + goal, build a BLIND prompt, call the LLM,
    parse structured output (``biggest_gap`` + ``coverage_verdict``).  Returns
    ``None`` (never a fabricated passing grade) when the LLM is unavailable or
    its output is unparseable after a retry — the caller FAILS CLOSED on None.
  * ``blind_merge_grade_satisfied(version, candidate_sha)`` — True only when a
    persisted grade for exactly ``candidate_sha`` carries a non-empty
    ``biggest_gap`` and ``coverage_verdict``.
  * ``persist_blind_merge_grade(version, record)`` — merge the grade into
    ``version.last_qualification`` under the stable ``blind_merge_grade`` key,
    calling ``flag_modified`` so SQLAlchemy detects the JSONB mutation.
  * ``blind_merge_grade_disabled()`` — operator escape hatch
    (``DISABLE_BLIND_MERGE_GRADE=1``) consistent with the existing
    ``DISABLE_LLM_*`` opt-outs.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import github, release_version
from app.landed_signal import landed_signal_predicate
from app.models import Project, Task, TaskStatus
from app.time_utils import _utcnow


async def _resolve_github_with_session(session, project):
    """Module-level wrapper so tests can patch app.blind_merge_grade._resolve_github_with_session.

    The real implementation is imported lazily inside to avoid a
    blind_merge_grade <-> release_promotion import cycle at module load.
    """
    from app.release_promotion import (
        _resolve_github_with_session as _fn,
    )

    return await _fn(session, project)


logger = logging.getLogger(__name__)

# The stable sub-key under ``version.last_qualification`` where the grade lives.
BLIND_MERGE_GRADE_KEY = "blind_merge_grade"

# Default to the latest capable Claude model id — goal-coverage grading is a
# load-bearing judgment in the core release path, so it gets the strongest
# model rather than the cheap classifier tier used by the review categoriser.
# Overridable without a code change for cost tuning.
_DEFAULT_MODEL = "claude-opus-4-8"
_GRADE_TIMEOUT = 90.0
_GRADE_MAX_TOKENS = 1024

# Coverage verdict vocabulary the grader must answer in.  Ordered worst -> best.
_VALID_VERDICTS: frozenset[str] = frozenset({"misses", "partial", "covers"})

# Bounds on the assembled diff text so a large release rollup does not blow the
# model's context window or the request cost.  Per-file and total caps; PRs are
# capped too so a pathological version does not page through hundreds of PRs.
_MAX_PRS = 60
_MAX_PER_FILE_PATCH_CHARS = 6_000
_MAX_TOTAL_DIFF_CHARS = 120_000


def _model_id() -> str:
    return os.environ.get("BLIND_MERGE_GRADE_MODEL", "").strip() or _DEFAULT_MODEL


def blind_merge_grade_disabled() -> bool:
    """Operator/env escape hatch — treats the gate as skipped/satisfied.

    Mirrors the existing ``DISABLE_LLM_*`` opt-outs.  When set the qualifier does
    NOT compute or require a grade, so a broken model account or an emergency
    promotion is never permanently wedged by this gate.
    """
    return os.environ.get("DISABLE_BLIND_MERGE_GRADE", "").strip() == "1"


def blind_merge_grade_satisfied(
    version: release_version.ReleaseVersion, candidate_sha: str | None
) -> bool:
    """True only if a usable blind merge-grade is persisted for ``candidate_sha``.

    "Usable" = the record's ``candidate_sha`` matches AND it carries a non-empty
    ``biggest_gap`` and a ``coverage_verdict``.  A skip marker (escape hatch) or
    a record for a different SHA does NOT satisfy the gate — a new candidate
    commit must be graded afresh.
    """
    if not candidate_sha:
        return False
    record = (version.last_qualification or {}).get(BLIND_MERGE_GRADE_KEY)
    if not isinstance(record, dict):
        return False
    if record.get("candidate_sha") != candidate_sha:
        return False
    if record.get("skipped_pr_count", 0) > 0:
        return False
    biggest_gap = record.get("biggest_gap")
    verdict = record.get("coverage_verdict")
    return bool(
        isinstance(biggest_gap, str)
        and biggest_gap.strip()
        and isinstance(verdict, str)
        and verdict.strip()
    )


def persist_blind_merge_grade(
    version: release_version.ReleaseVersion, record: dict[str, Any]
) -> None:
    """Merge ``record`` into ``version.last_qualification[blind_merge_grade]``.

    Uses ``flag_modified`` so SQLAlchemy detects the in-place JSONB mutation —
    the caller is responsible for adding the version to the session / commit.
    """
    qual = dict(version.last_qualification or {})
    qual[BLIND_MERGE_GRADE_KEY] = record
    version.last_qualification = qual
    flag_modified(version, "last_qualification")


async def _gather_merged_diff_text(
    session: AsyncSession,
    project: Project,
    version: release_version.ReleaseVersion,
    *,
    token: str,
    owner: str,
    repo: str,
) -> tuple[str, int, int]:
    """Assemble the merged-diff text for the version's landed PRs.

    Mirrors ``release_qualifier._fetch_version_theme_evidence``'s query: COMPLETED
    + landed tasks since ``version.opened_at``, read each PR number via
    ``task.first_payload``, fetch per-PR file patches.  Returns
    ``(diff_text, pr_count, skipped_pr_count)``.  Bounded by ``_MAX_PRS`` / per-file / total caps.
    """
    if version.opened_at is None:
        return ("", 0, 0)

    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == version.project_id,
                    Task.status == TaskStatus.COMPLETED,
                    landed_signal_predicate(session),
                    Task.updated_at >= version.opened_at,
                )
            )
        )
        .scalars()
        .all()
    )

    pr_numbers: list[int] = []
    seen: set[int] = set()
    for t in rows:
        if release_version._task_completed_at(t) < version.opened_at:
            continue
        pn = (t.first_payload or {}).get("pr_number")
        if isinstance(pn, int) and pn > 0 and pn not in seen:
            seen.add(pn)
            pr_numbers.append(pn)
    pr_numbers.sort()

    parts: list[str] = []
    total = 0
    graded_prs = 0
    skipped_prs = 0
    for pn in pr_numbers[:_MAX_PRS]:
        try:
            files = await github.get_pr_files(
                token, owner, repo, pn, include_patch=True
            )
        except Exception:  # noqa: BLE001 — a single PR fetch failure must not abort the grade
            logger.warning(
                "[BLIND_MERGE_GRADE] could not fetch files for PR #%s — skipping",
                pn,
            )
            skipped_prs += 1
            continue
        graded_prs += 1
        parts.append(f"### PR #{pn}")
        for f in files:
            filename = str(f.get("filename", "") or "")
            additions = int(f.get("additions", 0) or 0)
            deletions = int(f.get("deletions", 0) or 0)
            patch = f.get("patch")
            header = f"--- {filename} (+{additions}/-{deletions})"
            parts.append(header)
            if isinstance(patch, str) and patch:
                snippet = patch[:_MAX_PER_FILE_PATCH_CHARS]
                if len(patch) > _MAX_PER_FILE_PATCH_CHARS:
                    snippet += "\n…[patch truncated]…"
                parts.append(snippet)
            total += len(header) + (len(patch) if isinstance(patch, str) else 0)
        if total >= _MAX_TOTAL_DIFF_CHARS:
            parts.append("\n…[remaining PRs truncated for length]…")
            break

    return ("\n".join(parts), graded_prs, skipped_prs)


def _build_prompt(goal: str, diff_text: str, pr_count: int) -> str:
    """Build the BLIND grade prompt — goal + merged diffs ONLY.

    Deliberately contains nothing about the strategist's running_update or any
    self-reported progress: blindness is enforced by construction here.
    """
    goal_text = (goal or "").strip() or "(no version goal text was recorded)"
    diffs = diff_text.strip() or "(no merged diffs were found for this version)"
    return (
        "You are an UNINVESTED release grader. You have NOT worked on this "
        "release and you have NO access to any progress narrative. Judge ONLY "
        "the merged code diffs below against the stated version goal. Do not "
        "give the benefit of the doubt; grade what the diffs actually deliver, "
        "not what they intend.\n\n"
        f"VERSION GOAL:\n{goal_text}\n\n"
        f"MERGED DIFFS ({pr_count} merged PR(s) in this version):\n{diffs}\n\n"
        "Answer two questions:\n"
        "1. coverage_verdict: does the merged work cover the goal? One of "
        'exactly: "covers" (the goal is substantially delivered), "partial" '
        '(meaningful progress but important pieces are missing), or "misses" '
        "(the goal is essentially not delivered by these diffs).\n"
        "2. biggest_gap: in one or two sentences, name the SINGLE biggest thing "
        "the merged diffs do NOT deliver versus the goal. If the goal is fully "
        "covered, state what the weakest-covered aspect is instead — never leave "
        "this empty.\n\n"
        "Respond with ONLY valid JSON in this exact shape (no markdown, no "
        "commentary):\n"
        '{"coverage_verdict": "covers|partial|misses", "biggest_gap": "..."}'
    )


def _parse_grade(raw: str) -> dict[str, Any]:
    """Parse + validate the model's JSON response. Raises on anything unusable."""
    data = json.loads(raw)
    verdict = str(data.get("coverage_verdict", "")).strip().lower()
    biggest_gap = str(data.get("biggest_gap", "")).strip()
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"unknown coverage_verdict: {verdict!r}")
    if not biggest_gap:
        raise ValueError("empty biggest_gap")
    return {"coverage_verdict": verdict, "biggest_gap": biggest_gap}


async def compute_blind_merge_grade(
    session: AsyncSession,
    project: Project,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
) -> dict[str, Any] | None:
    """Compute a blind merge-grade for ``candidate_sha``.

    Returns a record ``{candidate_sha, coverage_verdict, biggest_gap, model,
    pr_count, graded_at}`` on success, or ``None`` when the grade cannot be
    produced (no API key, GitHub creds unresolved, or the model is
    unavailable/unparseable after a retry).  A ``None`` return must FAIL CLOSED
    at the call site — it never fabricates a passing grade.
    """
    goal = (version.goal or "").strip()
    if not goal:
        logger.warning(
            "[BLIND_MERGE_GRADE] version %s has no goal text — cannot grade candidate %s "
            "(grade unavailable; will retry on next qualification)",
            getattr(version, "version_number", "?"),
            (candidate_sha or "")[:8],
        )
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.info(
            "[BLIND_MERGE_GRADE] no ANTHROPIC_API_KEY — cannot grade v%s "
            "candidate %s (fail closed)",
            getattr(version, "version_number", "?"),
            (candidate_sha or "")[:8],
        )
        return None

    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        logger.info(
            "[BLIND_MERGE_GRADE] could not resolve GitHub creds for project %s "
            "— cannot grade (fail closed)",
            project.id,
        )
        return None
    token, owner, repo = resolved

    try:
        diff_text, pr_count, skipped_pr_count = await _gather_merged_diff_text(
            session, project, version, token=token, owner=owner, repo=repo
        )
    except Exception as exc:  # noqa: BLE001 — diff assembly failure defers (fail closed)
        logger.warning(
            "[BLIND_MERGE_GRADE] merged-diff assembly failed for v%s: %s",
            getattr(version, "version_number", "?"),
            exc,
        )
        return None

    prompt = _build_prompt(goal, diff_text, pr_count)
    model = _model_id()

    # One JSON-parse retry, then fail closed.  A parse error is retryable (the
    # model may have wrapped the JSON); any other exception is a transient/auth
    # failure that should defer immediately rather than re-roll the same call.
    parsed = await _grade_once(api_key, model, prompt, allow_retry=True)
    if parsed is None:
        return None
    return {
        "candidate_sha": candidate_sha,
        "coverage_verdict": parsed["coverage_verdict"],
        "biggest_gap": parsed["biggest_gap"],
        "model": model,
        "pr_count": pr_count,
        "skipped_pr_count": skipped_pr_count,
        "graded_at": _utcnow().isoformat(),
    }


async def _grade_once(
    api_key: str, model: str, prompt: str, *, allow_retry: bool
) -> dict[str, Any] | None:
    """Single LLM grade attempt. Returns parsed ``{coverage_verdict, biggest_gap}``
    or ``None`` on any failure (after one parse-error retry when ``allow_retry``)."""
    try:
        import anthropic as _anthropic

        client = _anthropic.AsyncAnthropic(api_key=api_key)
        resp = await client.messages.create(
            model=model,
            max_tokens=_GRADE_MAX_TOKENS,
            timeout=_GRADE_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        return _parse_grade(raw)
    except (json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
        if allow_retry:
            logger.info("[BLIND_MERGE_GRADE] parse error %s — retrying once", exc)
            return await _grade_once(api_key, model, prompt, allow_retry=False)
        logger.warning(
            "[BLIND_MERGE_GRADE] parse error on retry — failing closed: %s", exc
        )
        return None
    except Exception as exc:  # noqa: BLE001 — transient LLM failure defers
        logger.warning("[BLIND_MERGE_GRADE] LLM call failed (%s) — failing closed", exc)
        return None
