"""Merge-summary comment formatting and posting.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

from typing import Any

from app.github import logger


def build_merge_summary_body(
    *,
    task_type: str = "",
    scope: str = "",
    iteration_count: int = 0,
    provider: str = "",
    model: str = "",
    cost_usd: float | None = None,
    direction_title: str = "",
    direction_id: str = "",
) -> str:
    """Build a structured markdown merge summary for posting as a PR comment."""
    headline_parts: list[str] = []
    if task_type:
        headline_parts.append(task_type)
    if scope:
        headline_parts.append(scope)
    if iteration_count > 1:
        headline_parts.append(f"{iteration_count} review cycles")
    if cost_usd is not None:
        headline_parts.append(f"~${cost_usd:.2f}")
    headline = " · ".join(headline_parts) if headline_parts else "merge complete"

    detail_lines: list[str] = []
    if provider or model:
        detail_lines.append(f"**Model:** {' / '.join(filter(None, [provider, model]))}")
    if direction_title or direction_id:
        if direction_title and direction_id:
            detail_lines.append(
                f"**Direction:** {direction_title[:80]} `{direction_id[:8]}`"
            )
        else:
            detail_lines.append(f"**Direction:** {direction_title or direction_id}")

    if not detail_lines:
        return f"🤖 Blueprint: {headline}"

    return (
        f"<details>\n<summary>🤖 Blueprint: {headline}</summary>\n\n"
        + "\n".join(detail_lines)
        + "\n\n</details>"
    )


async def post_merge_summary_comment(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    task_type: str = "",
    scope: str = "",
    iteration_count: int = 0,
    provider: str = "",
    model: str = "",
    cost_usd: float | None = None,
    direction_title: str = "",
    direction_id: str = "",
) -> bool:
    """Post a one-time merge summary comment on a Blueprint-created PR.

    Best-effort: returns True on success, False on any failure.
    Only call this for Blueprint-authored PRs — skip manual PRs at the call site.
    """
    body = build_merge_summary_body(
        task_type=task_type,
        scope=scope,
        iteration_count=iteration_count,
        provider=provider,
        model=model,
        cost_usd=cost_usd,
        direction_title=direction_title,
        direction_id=direction_id,
    )
    from app import github as _gh

    try:
        await _gh.create_pr_comment(token, owner, repo, pr_number, body)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[GITHUB] post_merge_summary_comment failed for PR #{pr_number}: {exc}"
        )
        return False


BLUEPRINT_PR_MERGE_SUMMARY_MARKER = "<!-- blueprint:merge-summary -->"


def build_blueprint_pr_merge_summary_comment(
    task: Any,
    *,
    iteration_count: int | None = None,
    direction_title: str | None = None,
) -> str:
    """Build a markdown merge-summary comment for Blueprint-authored PRs."""
    from app.max_plan_pool import get_task_llm_provider
    from app.task_completion import _estimated_task_cost_usd

    ctx, pld = task.context or {}, task.first_payload or {}
    if iteration_count is None and isinstance(
        (ctx.get("review_loop") or {}).get("iteration"), int
    ):
        iteration_count = int((ctx.get("review_loop") or {})["iteration"])
    if not direction_title and isinstance(ctx.get("direction_title"), str):
        direction_title = ctx.get("direction_title")
    task_type, scope = (
        str(pld.get("type") or "create"),
        str(ctx.get("scope") or pld.get("scope") or "unknown"),
    )
    provider = get_task_llm_provider(task)
    model = ctx.get("llm_model") if isinstance(ctx.get("llm_model"), str) else None
    exec_label, cost = (
        " / ".join(p for p in [provider, model] if p) or "unknown",
        _estimated_task_cost_usd(task),
    )
    iters, dlabel = (
        iteration_count if isinstance(iteration_count, int) else "n/a",
        (direction_title or "n/a").strip() or "n/a",
    )
    top = f"Review-fix iterations: **{iters}**" + (
        f" · Cost: **~${cost:.2f}**" if cost is not None else ""
    )
    return (
        f"{BLUEPRINT_PR_MERGE_SUMMARY_MARKER}\n### Blueprint Merge Summary\n{top}\n\n"
        f"<details><summary>Execution details</summary>\n\n- Task: `{task_type}` (`{scope}`)\n"
        f"- Agent: `{ctx.get('agent_handler') or task_type}`\n- Provider/Model: `{exec_label}`\n- Direction: {dlabel[:120]}\n</details>"
    )


async def post_pipeline_merge_summary_comment(
    orig_task: Any,
    ci_task: Any,
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> None:
    """Post a one-time merge summary comment on a Blueprint-merged PR (errors swallowed)."""
    from app.task_completion import _estimated_task_cost_usd

    if (orig_task.created_by or "").upper() == "USER":
        return
    ctx, pld = orig_task.context or {}, orig_task.first_payload or {}
    iterations = int(
        ((ci_task.context or {}).get("review_loop") or {}).get("iteration") or 0
    )
    cost = _estimated_task_cost_usd(orig_task)
    provider, model = ctx.get("llm_provider") or "", ctx.get("llm_model") or ""
    task_type, scope = (
        pld.get("type", "create"),
        ctx.get("scope") or pld.get("scope") or "",
    )
    direction_id, direction_title = (
        ctx.get("direction_id"),
        ctx.get("direction_title") or "",
    )
    sparts = (
        [f"`{task_type}`"]
        + ([f"`{scope}` scope"] if scope else [])
        + (
            ["1 review cycle" if iterations == 1 else f"{iterations} review cycles"]
            if iterations > 0
            else []
        )
        + ([f"~${cost:.2f}"] if cost is not None else [])
    )
    dparts = (
        [f"`{'/'.join(filter(None, [provider, model]))}`"] if provider or model else []
    )
    if direction_id:
        dparts.append(
            f"direction: {direction_title[:60] if direction_title else str(direction_id)[:8]}"
        )
    body = f"**Blueprint** · {' · '.join(sparts)}" + (
        "\n" + " · ".join(dparts) if dparts else ""
    )
    from app import github as _gh

    try:
        await _gh.create_pr_comment(gh_token, owner, repo, pr_number, body)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] merge-summary comment failed for PR #{pr_number}: {e}"
        )
