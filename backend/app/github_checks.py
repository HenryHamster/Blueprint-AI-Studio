"""CI check-run / commit-status operations plus the short-lived check-runs cache.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

import asyncio
import time

from fastapi import HTTPException, status

from app.github import logger


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time so
    monkeypatching ``app.github._github_request`` stays effective here."""
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


async def update_commit_status(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    state: str,
    description: str = "",
    context: str = "blueprint-agent",
    target_url: str = "",
) -> dict:
    """
    Reports agent task progress (Queued, In Progress, Success, Failed) directly to the GitHub Commit Status API.
    state can be one of: 'error', 'failure', 'pending', or 'success'.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload = {"state": state, "description": description, "context": context}
    if target_url:
        payload["target_url"] = target_url

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/statuses/{sha}",
        headers,
        json=payload,
    )

    if response.status_code != 201:
        logger.error(f"Failed to update commit status: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update commit status on GitHub.",
        )

    return response.json()


async def create_check_run(
    token: str,
    owner: str,
    repo: str,
    head_sha: str,
    name: str = "Blueprint Code Review",
    details_url: str = "",
) -> dict | None:
    """Create a Check Run for a commit in 'in_progress' state.

    Returns the check run dict (including its id) on success, or None on error.
    Uses the Checks API (POST /repos/{owner}/{repo}/check-runs).
    Requires a GitHub App token with checks:write permission.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Audit W1/F8: use _utcnow() for consistency with the rest of the app.
    from app.time_utils import _utcnow as _ut

    payload: dict = {
        "name": name,
        "head_sha": head_sha,
        "status": "in_progress",
        "started_at": _ut().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if details_url:
        payload["details_url"] = details_url

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/check-runs",
        headers,
        json=payload,
    )

    if response.status_code == 201:
        return response.json()
    logger.warning(
        f"[GITHUB] create_check_run failed for {owner}/{repo} sha={head_sha[:8]}: "
        f"{response.status_code} {response.text[:200]}"
    )
    return None


async def update_check_run(
    token: str,
    owner: str,
    repo: str,
    check_run_id: int,
    conclusion: str,
    title: str,
    summary: str,
    annotations: list[dict] | None = None,
) -> dict | None:
    """Update an existing Check Run to 'completed' with a conclusion.

    conclusion must be one of: 'success', 'failure', 'neutral', 'cancelled',
    'skipped', 'timed_out', 'action_required'.

    annotations is an optional list of dicts with keys:
        path, start_line, end_line, annotation_level ('notice'|'warning'|'failure'),
        message.  GitHub caps annotations at 50 per request.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    output: dict = {
        "title": title,
        "summary": summary[:65535],
    }
    if annotations:
        output["annotations"] = annotations[:50]

    # Audit W1/F8: use _utcnow() for consistency with the rest of the app.
    from app.time_utils import _utcnow as _ut

    payload: dict = {
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": _ut().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output": output,
    }

    response = await _github_request(
        "PATCH",
        f"https://api.github.com/repos/{owner}/{repo}/check-runs/{check_run_id}",
        headers,
        json=payload,
    )

    if response.status_code == 200:
        return response.json()
    logger.warning(
        f"[GITHUB] update_check_run failed for {owner}/{repo} run_id={check_run_id}: "
        f"{response.status_code} {response.text[:200]}"
    )
    return None


def _unique(*values: int | None) -> list[int]:
    """Return deduplicated non-None values preserving order."""
    seen: set[int] = set()
    result: list[int] = []
    for v in values:
        if v is not None and v not in seen:
            seen.add(v)
            result.append(v)
    return result


async def get_check_runs(
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
    *,
    treat_missing_as_success: bool = True,
) -> dict:
    """Get CI check run results for a commit SHA.

    Returns:
        {"status": "completed"|"in_progress"|"pending"|"no_checks",
         "conclusion": "success"|"failure"|None,
         "details": [{"name": str, "status": str, "conclusion": str|None}],
         "failed_logs": str}

    ``treat_missing_as_success`` (opt-in, default True — preserves the historic
    behaviour for every existing caller): when a commit has NO check-runs at
    all this fails OPEN (``status="completed", conclusion="success"``) so the
    orchestrator's CI-wait loop and the webhook handlers do not hang forever on
    repos that simply have no CI configured.  Pass ``False`` to make an empty
    check-runs list resolve to a distinct ``status="no_checks"`` (conclusion
    ``None``) instead — the release qualifier passes this so it can tell
    'no CI ever verified this commit' apart from 'CI ran and passed' and
    refuse to promote on the absence of a signal.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    check_runs = []
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}/check-runs?per_page=100"

    while url:
        response = await _github_request(
            "GET",
            url,
            headers,
            project_id=project_id,
        )
        if response.status_code != 200:
            logger.error(f"Failed to get check runs for {ref}: {response.text}")
            return {
                "status": "pending",
                "conclusion": None,
                "details": [],
                "failed_logs": "",
            }

        data = response.json()
        check_runs.extend(data.get("check_runs", []))

        # Follow pagination link to next page
        url = response.links.get("next", {}).get("url")

    if not check_runs:
        if not treat_missing_as_success:
            # Strict mode (opt-in — the release qualifier).  An empty
            # check-runs list is NOT a pass: the caller must distinguish
            # 'no CI ran on this commit' from 'CI ran and passed', so a
            # promotion can never fire purely because nothing verified the
            # candidate.  Reported as a distinct "no_checks" status.
            return {
                "status": "no_checks",
                "conclusion": None,
                "details": [],
                "failed_logs": "",
            }
        # No CI configured on this repo — treat as passed so the
        # orchestrator doesn't wait 30 min for checks that will never arrive.
        return {
            "status": "completed",
            "conclusion": "success",
            "details": [],
            "failed_logs": "",
        }

    details = []
    failed_names = []
    for cr in check_runs:
        details.append(
            {
                "name": cr.get("name", ""),
                "status": cr.get("status", ""),
                "conclusion": cr.get("conclusion"),
            }
        )
        if cr.get("conclusion") == "failure":
            failed_names.append(cr.get("name", "unknown"))

    # Aggregate status
    statuses = [cr.get("status", "") for cr in check_runs]
    agg_status = (
        "in_progress"
        if any(s in ("queued", "in_progress") for s in statuses)
        else "completed"
    )

    # Aggregate conclusion — "skipped" and "neutral" are non-failures
    conclusions = [
        cr.get("conclusion") for cr in check_runs if cr.get("status") == "completed"
    ]
    if not conclusions:
        agg_conclusion = None
    elif all(c in ("success", "skipped", "neutral") for c in conclusions):
        agg_conclusion = "success"
    else:
        agg_conclusion = "failure"

    # Collect failure details — prefer actual job logs, fall back to annotations
    failed_logs = ""
    if failed_names:
        log_lines = []

        # Pre-fetch actual job IDs via workflow runs API (check run ID != job ID sometimes)
        job_id_by_name: dict[str, int] = {}
        try:
            runs_resp = await _github_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs?head_sha={ref}&per_page=5",
                headers,
                timeout=5.0,
            )
            if runs_resp.status_code == 200:
                wf_runs = runs_resp.json().get("workflow_runs", [])
                jobs_coros = [
                    _github_request(
                        "GET",
                        f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{wf_run['id']}/jobs?filter=latest&per_page=30",
                        headers,
                        timeout=5.0,
                    )
                    for wf_run in wf_runs
                ]
                jobs_results = await asyncio.gather(*jobs_coros, return_exceptions=True)
                for result in jobs_results:
                    if isinstance(result, Exception) or result.status_code != 200:
                        continue
                    for job in result.json().get("jobs", []):
                        if job.get("conclusion") == "failure":
                            job_id_by_name[job.get("name", "")] = job["id"]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[GITHUB] Workflow runs pre-fetch failed for {ref}: {exc}")

        for cr in check_runs:
            if cr.get("conclusion") != "failure":
                continue
            cr_id = cr.get("id")
            name = cr.get("name", "unknown")

            # Try to fetch actual job logs (check run ID often == job ID)
            got_logs = False
            for job_id in _unique(cr_id, job_id_by_name.get(name)):
                try:
                    log_response = await _github_request(
                        "GET",
                        f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                        headers,
                        follow_redirects=True,
                        timeout=10.0,
                    )
                    if log_response.status_code == 200:
                        raw_log = log_response.text
                        lines = raw_log.strip().split("\n")
                        error_lines = [
                            ln
                            for ln in lines
                            if any(
                                kw in ln.lower()
                                for kw in ["error", "failed", "err!", "✗", "\u00d7"]
                            )
                        ]
                        if error_lines:
                            log_lines.append(
                                f"  [{name}] Errors:\n"
                                + "\n".join(
                                    f"    {ln.strip()}" for ln in error_lines[-30:]
                                )
                            )
                        else:
                            log_lines.append(
                                f"  [{name}] (last 30 lines):\n"
                                + "\n".join(f"    {ln.strip()}" for ln in lines[-30:])
                            )
                        got_logs = True
                        break
                except Exception:
                    logger.exception(
                        f"[CI LOGS] Failed to fetch logs for check-run {name}"
                    )
            if got_logs:
                continue

            # Fallback: fetch annotations (errors/failures only — skip warnings like deprecation notices)
            try:
                ann_response = await _github_request(
                    "GET",
                    f"https://api.github.com/repos/{owner}/{repo}/check-runs/{cr_id}/annotations",
                    headers,
                )
                if ann_response.status_code == 200:
                    for ann in ann_response.json():
                        if ann.get("annotation_level") not in ("failure", "error"):
                            continue
                        msg = ann.get("message", "")
                        path = ann.get("path", "")
                        line = ann.get("start_line", "")
                        log_lines.append(f"  [{name}] {path}:{line} — {msg}")
            except Exception:
                logger.exception(
                    f"[CI LOGS] Failed to fetch annotations for check-run {name}"
                )

            # Also include output summary if available
            output = cr.get("output", {})
            summary = output.get("summary", "") if output else ""
            if summary:
                log_lines.append(f"  [{name}] {summary[:500]}")

        failed_logs = (
            "\n".join(log_lines)
            if log_lines
            else "CI checks failed: " + ", ".join(failed_names)
        )

    return {
        "status": agg_status,
        "conclusion": agg_conclusion,
        "details": details,
        "failed_logs": failed_logs,
    }


# ─── Short-lived dedup cache for check-runs lookups ─────────────────────────
# The CI-wait loop in the orchestrator and the check_run webhook handler both
# fetch check-runs per task that's watching a PR.  When N sibling tasks share
# the same head_sha (a rollup with N children is the common case), each task
# was making its own API call — 20 children on one PR = 20 duplicate calls per
# poll interval.  A 30s TTL cache collapses siblings into one call without
# adding any cross-cycle staleness risk beyond the poll interval itself.
_CHECK_RUNS_CACHE_TTL_S = 30
_CHECK_RUNS_CACHE_MAX = 500  # entries; cheap eviction when oversized
_check_runs_cache: dict[tuple[str, str, str, bool], tuple[dict, float]] = {}


def invalidate_check_runs_cache(owner: str, repo: str, head_sha: str) -> None:
    """Drop the cached check-runs result for a SHA — call when the caller
    observes a transition it needs to see immediately (e.g. a webhook arrived
    indicating a new run started).  Safe to call when the key isn't present.

    Drops both the lenient and strict cache variants for the SHA."""
    _check_runs_cache.pop((owner, repo, head_sha, True), None)
    _check_runs_cache.pop((owner, repo, head_sha, False), None)


async def get_check_runs_cached(
    token: str,
    owner: str,
    repo: str,
    head_sha: str,
    *,
    project_id: str | None = None,
    ttl: float = _CHECK_RUNS_CACHE_TTL_S,
    treat_missing_as_success: bool = True,
) -> dict:
    """Cached wrapper around ``get_check_runs``.

    TTL is short (30s) so a fresh webhook or a status change is picked up on
    the next orchestrator cycle — the cache exists purely to deduplicate
    simultaneous fan-out from siblings watching the same PR, not to mask
    status changes over time.

    ``treat_missing_as_success`` is forwarded to ``get_check_runs`` (default
    True preserves the fail-open behaviour) and is part of the cache key so the
    lenient and strict interpretations never serve each other's result.
    """
    import app.shared_state as _ss
    from app import github as _gh

    key = (owner, repo, head_sha, treat_missing_as_success)
    now = time.time()
    cached = _check_runs_cache.get(key)
    if cached is not None and (now - cached[1]) < ttl:
        _ss.github_cache_hits["check_runs"] = (
            _ss.github_cache_hits.get("check_runs", 0) + 1
        )
        return cached[0]
    result = await _gh.get_check_runs(
        token,
        owner,
        repo,
        head_sha,
        project_id=project_id,
        treat_missing_as_success=treat_missing_as_success,
    )
    _check_runs_cache[key] = (result, now)
    # Lazy eviction: when the dict gets oversized, drop anything older than
    # 2x TTL.  Cheap, avoids unbounded growth across long-lived processes.
    if len(_check_runs_cache) > _CHECK_RUNS_CACHE_MAX:
        cutoff = now - (ttl * 2)
        for k in [k for k, (_, ts) in _check_runs_cache.items() if ts < cutoff]:
            _check_runs_cache.pop(k, None)
    return result


