"""CI check-run / commit-status operations plus the short-lived check-runs cache.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

import asyncio
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import HTTPException, status

from app.github import logger


# Pytest emits its authoritative failing-test list as ``FAILED <path>::<test>``
# lines (the "short test summary info" block). These are the selectors the
# ci_failure fix loop re-runs locally (agent ``./check`` → ``test-backend.sh
# --postgres``). The ``\S+::\S+`` shape isolates a real selector from captured
# log noise such as ``[STRATEGIC] ... FAILED (failure #1)`` (no ``::``).
_PYTEST_FAILED_SELECTOR_RE = re.compile(r"FAILED\s+(\S+::\S+)")


def _extract_pytest_selectors(raw_log: str, cap: int = 50) -> list[str]:
    """Deduped pytest ``path::test`` selectors from a raw CI job log.

    Scans the WHOLE log, not a trailing window. A test that exercises a failure
    path floods the job log with captured WARNING/ERROR lines containing
    ``error``/``failed``; the per-check ``error_lines[-30:]`` slice in
    :func:`get_check_runs` can be entirely such noise, pushing the real
    ``FAILED testing/...::test`` summary lines out of the window. When that
    happens the spawned fix task's ``ci_failure_logs`` carries no selectors, the
    agent's ci-failure ``./check`` gate (which re-runs the exact failing
    selectors via the Postgres harness) has nothing to verify, and the fix loop
    ships blind — the open-loop strand. Collecting selectors across the full log
    and surfacing them explicitly keeps that loop closed.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _PYTEST_FAILED_SELECTOR_RE.finditer(raw_log or ""):
        sel = m.group(1)
        if sel not in seen:
            seen.add(sel)
            out.append(sel)
            if len(out) >= cap:
                break
    return out


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time so
    monkeypatching ``app.github._github_request`` stays effective here."""
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


def _deferrable_quota_active() -> bool:
    """Delegate to app.github._deferrable_quota_active at call time."""
    from app import github as _gh

    return _gh._deferrable_quota_active()


# GitHub-generated setup-phase step names. These are emitted by GitHub Actions
# for every job (repo-agnostic, not workflow config). A job whose *only* failed
# steps are these failed during infra setup — container/runner init, before any
# user step ran — which is a transient infra flake, not a code defect.
_GITHUB_SETUP_STEPS = frozenset(
    {"Set up job", "Initialize containers", "Set up runner"}
)


def _job_failed_only_at_setup(job: dict) -> bool:
    """True when a failed job's failed steps are ALL GitHub built-in setup steps.

    That means it died during infra setup (container/runner init) before any
    user step ran — a transient infra flake, not a code defect. False when any
    failed step is real work, or when the job has no failed steps at all.
    """
    failed_steps = [
        s.get("name", "")
        for s in (job.get("steps") or [])
        if s.get("conclusion") == "failure"
    ]
    return bool(failed_steps) and all(s in _GITHUB_SETUP_STEPS for s in failed_steps)


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


def _details_have_genuine_failure(details: list) -> bool:
    """Return True when per-check *details* contain a genuinely failed check.

    GitHub Actions matrix shards emit their own check runs (``CI / Backend
    (shard N)``) AND the workflow's aggregate ``needs`` job (``CI / Backend``).
    When a shard is infrastructure-cancelled (or timed out / skipped) the
    aggregate job exits non-zero and reports ``failure`` even though NO shard
    genuinely failed — the aggregate conclusion launders the transient child.
    This is the confirmed v28 attempt-6 promotion failure: PR #16885 reported
    ``CI / Backend`` conclusion ``failure`` with zero failing backend tests.

    A check name ``X`` is treated as an aggregate parent when some OTHER name
    startswith ``X + " ("`` — the GitHub Actions matrix naming convention.
    Parent conclusions are derived from their children and are NOT trusted;
    only LEAF (non-parent) checks count their own ``failure`` /
    ``action_required``.  This discounts a laundered aggregate in one pass and
    also composes across nesting (a parent-of-parent still resolves to its
    leaf children).  A misclassification here only ever discounts a ``failure``
    that looks like a matrix aggregate — it can never invent one — and the
    release reconciler bounds any residual false-negative by escalating after
    its transient-red sweep cap.
    """
    names = [str(d.get("name") or "") for d in details if isinstance(d, dict)]
    genuine = {"failure", "action_required"}
    for d in details:
        if not isinstance(d, dict):
            continue
        if d.get("conclusion") not in genuine:
            continue
        name = str(d.get("name") or "")
        if any(other != name and other.startswith(f"{name} (") for other in names):
            # Aggregate parent — its failure is derived from its matrix children.
            continue
        return True
    return False


async def get_check_runs(
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
    *,
    treat_missing_as_success: bool = True,
    include_failed_logs: bool = True,
) -> dict:
    """Get CI check run results for a commit SHA.

    Returns:
        {"status": "completed"|"in_progress"|"pending"|"no_checks",
         "conclusion": "success"|"failure"|None,
         "details": [{"name": str, "status": str, "conclusion": str|None}],
         "failed_logs": str,
         "genuine_failure": bool}

    ``genuine_failure`` (additive — consumers use ``.get``): whether the
    completed checks contain at least one genuinely failed check
    (``failure``/``action_required``), computed by
    :func:`_details_have_genuine_failure` so an aggregate ``needs`` gate whose
    ``failure`` merely launders a cancelled/timed_out matrix shard reports
    ``False``.  The release reconciler uses this signal to keep a transient-red
    promotion pending instead of tearing it down.

    ``treat_missing_as_success`` (opt-in, default True — preserves the historic
    behaviour for every existing caller): when a commit has NO check-runs at
    all this fails OPEN (``status="completed", conclusion="success"``) so the
    orchestrator's CI-wait loop and the webhook handlers do not hang forever on
    repos that simply have no CI configured.  Pass ``False`` to make an empty
    check-runs list resolve to a distinct ``status="no_checks"`` (conclusion
    ``None``) instead — the release qualifier passes this so it can tell
    'no CI ever verified this commit' apart from 'CI ran and passed' and
    refuse to promote on the absence of a signal.

    ``include_failed_logs`` (default True — every existing caller keeps its
    behaviour): on a RED sha the failure-detail fan-out below fetches workflow
    runs + per-run jobs + per-failed-check job LOGS + annotations — 3-10+ extra
    calls against the SHARED 5000/hr install-token bucket, per poll. That is
    worth it for the fix loop (which consumes ``failed_logs``), and pure waste
    for a caller that only reads status/conclusion/details and DISCARDS the
    logs. Pass ``False`` to skip the fan-out entirely: the result is identical
    apart from ``failed_logs``/``setup_phase_failure``/``failed_run_ids``, which
    are returned empty.
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
            # Tag as an API-error result so callers can skip caching it.
            # This prevents a transient GitHub 5xx / rate-limit storm from
            # poisoning the 30 s dedup window with a fake 'pending' verdict.
            return {
                "status": "pending",
                "conclusion": None,
                "details": [],
                "failed_logs": "",
                "genuine_failure": False,
                "_from_api_error": True,
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
                "genuine_failure": False,
            }
        # No CI configured on this repo — treat as passed so the
        # orchestrator doesn't wait 30 min for checks that will never arrive.
        return {
            "status": "completed",
            "conclusion": "success",
            "details": [],
            "failed_logs": "",
            "genuine_failure": False,
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
        if any(
            s in ("queued", "in_progress", "waiting", "requested", "pending")
            for s in statuses
        )
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
    # setup_phase_failure: at least one failed job failed ONLY at GitHub built-in
    # setup steps (infra flake). failed_run_ids: the workflow run(s) to re-run.
    setup_phase_failure = False
    failed_run_ids: list[int] = []
    if failed_names and not include_failed_logs:
        # Caller reads only status/conclusion/details (e.g. the agent check-runs
        # proxy, which discards failed_logs) — skip the 3-10+ call failure fan-out
        # rather than burn shared-bucket quota producing logs nobody reads.
        return {
            "status": agg_status,
            "conclusion": agg_conclusion,
            "details": details,
            "failed_logs": "",
            "setup_phase_failure": False,
            "failed_run_ids": [],
            "genuine_failure": _details_have_genuine_failure(details),
        }
    if failed_names and _deferrable_quota_active():
        logger.warning(
            "[CI LOGS] quota floor active — skipping failure-detail fan-out for %s",
            ref[:8] if ref else ref,
        )
        return {
            "status": agg_status,
            "conclusion": agg_conclusion,
            "details": details,
            "failed_logs": "",
            "setup_phase_failure": False,
            "failed_run_ids": [],
            "genuine_failure": _details_have_genuine_failure(details),
        }
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
                for wf_run, result in zip(wf_runs, jobs_results, strict=True):
                    if isinstance(result, Exception) or result.status_code != 200:
                        continue
                    run_has_failed_job = False
                    for job in result.json().get("jobs", []):
                        if job.get("conclusion") != "failure":
                            continue
                        run_has_failed_job = True
                        job_id_by_name[job.get("name", "")] = job["id"]
                        if _job_failed_only_at_setup(job):
                            setup_phase_failure = True
                    if run_has_failed_job:
                        failed_run_ids.append(wf_run["id"])
            else:
                logger.warning(
                    f"[CI LOGS] workflow runs fetch for {ref[:8] if ref else ref} returned "
                    f"{runs_resp.status_code} — job-id resolution unavailable"
                )
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
                        # Authoritative pytest selectors first, collected across
                        # the WHOLE log so they survive the error_lines[-30:]
                        # truncation below (see _extract_pytest_selectors). The
                        # fix loop's ci-failure ./check re-runs exactly these.
                        selectors = _extract_pytest_selectors(raw_log)
                        error_lines = [
                            ln
                            for ln in lines
                            if any(
                                kw in ln.lower()
                                for kw in ["error", "failed", "err!", "✗", "\u00d7"]
                            )
                        ]
                        block: list[str] = []
                        if selectors:
                            block.append(
                                f"  [{name}] Failing tests:\n"
                                + "\n".join(f"    FAILED {s}" for s in selectors)
                            )
                        if error_lines:
                            block.append(
                                f"  [{name}] Errors:\n"
                                + "\n".join(
                                    f"    {ln.strip()}" for ln in error_lines[-30:]
                                )
                            )
                        elif not selectors:
                            block.append(
                                f"  [{name}] (last 30 lines):\n"
                                + "\n".join(f"    {ln.strip()}" for ln in lines[-30:])
                            )
                        log_lines.append("\n".join(block))
                        got_logs = True
                        break
                    else:
                        logger.warning(
                            f"[CI LOGS] job logs fetch for {name} returned "
                            f"{log_response.status_code} — selectors unavailable; "
                            f"falling back to annotations"
                        )
                except Exception:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
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
        "setup_phase_failure": setup_phase_failure,
        "failed_run_ids": failed_run_ids,
        "genuine_failure": _details_have_genuine_failure(details),
    }


# The agent VM image bake workflow file (.github/workflows/packer-agent.yml).
# The control plane reads this run's conclusion to learn whether the Packer bake
# for a promoted SHA succeeded — without changing the workflow itself.
AGENT_BAKE_WORKFLOW_FILE = "packer-agent.yml"

# The main CI workflow file (.github/workflows/ci.yml).  Its ``detect`` job forces
# frontend/backend/agent all-true on a ``workflow_dispatch``, so dispatching it on
# the integration branch runs the FULL 8-shard backend + frontend + agent matrix.
# The promotion_failed re-driver dispatches this to obtain a real full-suite green
# signal for the integration HEAD before re-entering qualification.
CI_WORKFLOW_FILE = "ci.yml"

# The deploy pipeline workflow file (.github/workflows/deploy.yml).  deploy.yml
# runs on push and deploys the pushed merge commit, so its Actions runs keyed on
# the promoted SHA are the evidence that distinguishes "deploy workflow never
# dispatched" from "it ran and failed" from "it ran and the revision is stale"
# when a promotion's serving confirmation times out with no new revision.
DEPLOY_WORKFLOW_FILE = "deploy.yml"


async def dispatch_workflow_run(
    token: str,
    owner: str,
    repo: str,
    workflow_file: str,
    ref: str,
    project_id: str | None = None,
    inputs: Mapping[str, str] | None = None,
) -> bool:
    """Trigger a ``workflow_dispatch`` run of ``workflow_file`` on ``ref``.

    POSTs to ``/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches``
    with body ``{"ref": ref, "inputs": inputs}`` when inputs are supplied,
    otherwise the legacy ``{"ref": ref}`` body. Returns True on GitHub's documented 204 success,
    False (with a warning log) on any other status or on any transport error.
    Never raises — graceful degradation, mirroring the sibling read-only helpers
    (:func:`get_workflow_run_for_sha`, :func:`rerun_failed_jobs`).  The workflow
    file must expose a ``workflow_dispatch:`` trigger for this to succeed.
    """
    outcome = await dispatch_workflow_run_outcome(
        token,
        owner,
        repo,
        workflow_file,
        ref,
        project_id=project_id,
        inputs=inputs,
    )
    return outcome == "accepted"


async def dispatch_workflow_run_outcome(
    token: str,
    owner: str,
    repo: str,
    workflow_file: str,
    ref: str,
    project_id: str | None = None,
    inputs: Mapping[str, str] | None = None,
) -> str:
    """Return accepted, rejected, or ambiguous for a workflow dispatch POST.

    GitHub's endpoint returns no run id or idempotency token. Transport errors,
    throttling, and server errors are therefore ambiguous: the server may have
    enqueued the run before the response was lost. Callers must reconcile a
    workflow-visible correlation id before retrying an ambiguous outcome.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = await _github_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
            f"{workflow_file}/dispatches",
            headers,
            json=(
                {"ref": ref, "inputs": dict(inputs)}
                if inputs is not None
                else {"ref": ref}
            ),
            project_id=project_id,
            timeout=10.0,
            max_retries=1,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[GITHUB] dispatch_workflow_run request failed for %s/%s workflow=%s ref=%s",
            owner,
            repo,
            workflow_file,
            ref,
        )
        return "ambiguous"
    if response.status_code == 204:
        return "accepted"
    logger.warning(
        "[GITHUB] dispatch_workflow_run: %s/%s workflow=%s ref=%s returned %s: %s",
        owner,
        repo,
        workflow_file,
        ref,
        response.status_code,
        response.text[:200],
    )
    if 400 <= response.status_code < 500 and response.status_code not in {
        408,
        409,
        425,
        429,
    }:
        return "rejected"
    return "ambiguous"


async def get_workflow_run_for_dispatch_request(
    token: str,
    owner: str,
    repo: str,
    workflow_file: str,
    ref: str,
    request_id: str,
    candidate_sha: str,
    *,
    project_id: str | None = None,
    max_pages: int = 5,
) -> dict:
    """Reconcile a dispatch by the request id embedded in its trusted run title."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    expected_title = f"Prepromotion rehearsal {request_id} {candidate_sha}"
    bounded_pages = max(1, min(int(max_pages), 10))
    for page in range(1, bounded_pages + 1):
        query = urlencode(
            {
                "branch": ref,
                "event": "workflow_dispatch",
                "per_page": 100,
                "page": page,
            }
        )
        try:
            response = await _github_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
                f"{workflow_file}/runs?{query}",
                headers,
                project_id=project_id,
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001
            return {"state": "unavailable", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        if response.status_code != 200:
            return {
                "state": "unavailable",
                "detail": f"github returned {response.status_code}",
            }
        try:
            runs = response.json().get("workflow_runs", [])
        except (ValueError, TypeError, AttributeError):
            return {"state": "unavailable", "detail": "malformed workflow-runs response"}
        if not isinstance(runs, list):
            return {"state": "unavailable", "detail": "malformed workflow-runs response"}
        for run in runs:
            if not isinstance(run, dict) or run.get("display_title") != expected_title:
                continue
            run_id = run.get("id")
            if isinstance(run_id, bool) or not isinstance(run_id, int):
                return {"state": "unavailable", "detail": "matched run has invalid id"}
            return {
                "state": "found",
                "run_id": run_id,
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "html_url": run.get("html_url", ""),
                "head_sha": run.get("head_sha"),
                "head_branch": run.get("head_branch"),
                "event": run.get("event"),
                "path": run.get("path"),
                "run_attempt": run.get("run_attempt"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
            }
        if len(runs) < 100:
            return {"state": "not_found"}
    return {"state": "not_found"}


async def get_workflow_run_for_sha(
    token: str,
    owner: str,
    repo: str,
    head_sha: str,
    *,
    workflow_file: str = AGENT_BAKE_WORKFLOW_FILE,
    project_id: str | None = None,
) -> dict | None:
    """Return the most recent GitHub Actions run of ``workflow_file`` for ``head_sha``.

    Wraps ``GET /repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs``
    filtered by ``head_sha``.  Used to learn the agent VM image bake outcome for
    a promoted commit: the serving gate needs to know whether the Packer bake
    that a rebake-expecting SHA triggered actually ran and succeeded.

    Returns a small status dict for the newest matching run::

        {"run_id": int, "status": str, "conclusion": str | None, "html_url": str}

    ``status`` is one of GitHub's run statuses (``queued`` / ``in_progress`` /
    ``completed``); ``conclusion`` is ``success`` / ``failure`` / ``cancelled`` /
    ... or ``None`` while the run is still in flight.  Returns ``None`` when no
    run exists for the SHA, or on any API error / unparseable body — graceful
    degradation, never raises (mirrors the other read-only github helpers).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _deferrable_quota_active():
        logger.warning(
            "[GITHUB] get_workflow_run_for_sha: quota floor active — returning None for %s",
            head_sha[:8] if head_sha else head_sha,
        )
        return None
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
        f"{workflow_file}/runs?head_sha={head_sha}&per_page=20",
        headers,
        project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(
            "[GITHUB] get_workflow_run_for_sha: %s/%s workflow=%s sha=%s returned %s",
            owner,
            repo,
            workflow_file,
            head_sha[:8] if head_sha else head_sha,
            response.status_code,
        )
        return None
    try:
        runs = response.json().get("workflow_runs", [])
    except (ValueError, TypeError):
        logger.warning(
            "[GITHUB] get_workflow_run_for_sha: unparseable body for %s/%s sha=%s",
            owner,
            repo,
            head_sha[:8] if head_sha else head_sha,
        )
        return None
    if not runs:
        return None
    # GitHub returns most-recent-first, but sort defensively by created_at
    # (ISO-8601 sorts lexically) so a re-run / dispatch can't surface an older run.
    runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    run = runs[0]
    return {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "html_url": run.get("html_url", ""),
    }


def _parse_github_timestamp(value) -> datetime | None:
    """Parse GitHub's ISO-8601 UTC timestamp (``…T…Z``) defensively.

    Returns ``None`` on any unparseable / missing value so callers degrade
    gracefully instead of raising.  ``Z`` is replaced with ``+00:00`` so the
    parse works on Python versions where ``datetime.fromisoformat`` does not yet
    accept the trailing ``Z``.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


async def get_latest_inflight_workflow_run_for_branch(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    *,
    workflow_file: str = CI_WORKFLOW_FILE,
    project_id: str | None = None,
    max_age_seconds: int | None = None,
) -> dict | None:
    """Return the newest queued/in-progress ``workflow_file`` run on ``branch``.

    ci.yml is keyed to a branch-level ``concurrency`` group
    (``cancel-in-progress``), so on an actively-merging integration branch a
    fresh ``workflow_dispatch`` cancels whatever full-suite run is already in
    flight.  This helper is the coalescing seam for that policy: before
    dispatching a run for a NEW integration HEAD, the caller asks whether a
    full-suite run is ALREADY queued/in-progress on the branch and, if so, waits
    for that run to conclude instead of cancelling it with yet another dispatch.
    Without this, dispatched runs on a busy branch rarely survive to a
    conclusion and the full-suite signal can never turn green.

    Wraps ``GET /repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs?branch={branch}``
    and returns the newest run whose ``status`` is ``queued`` or ``in_progress``,
    optionally restricted to runs created within the last ``max_age_seconds``
    (so a stuck / zombie run cannot block fresh dispatches forever).

    Returns a small dict::

        {"run_id": int, "status": str, "conclusion": str | None,
         "html_url": str, "head_sha": str, "created_at": str}

    or ``None`` when no qualifying run exists, or on any API error / unparseable
    body — graceful degradation, never raises (mirrors the sibling read-only
    github helpers).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _deferrable_quota_active():
        logger.warning(
            "[GITHUB] get_latest_inflight_workflow_run_for_branch: quota floor "
            "active — returning None for %s/%s branch=%s",
            owner,
            repo,
            branch,
        )
        return None
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
        f"{workflow_file}/runs?branch={branch}&per_page=20",
        headers,
        project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(
            "[GITHUB] get_latest_inflight_workflow_run_for_branch: %s/%s "
            "workflow=%s branch=%s returned %s",
            owner,
            repo,
            workflow_file,
            branch,
            response.status_code,
        )
        return None
    try:
        runs = response.json().get("workflow_runs", [])
    except (ValueError, TypeError):
        logger.warning(
            "[GITHUB] get_latest_inflight_workflow_run_for_branch: unparseable "
            "body for %s/%s branch=%s",
            owner,
            repo,
            branch,
        )
        return None
    now = datetime.now(timezone.utc)
    cutoff = (
        now - timedelta(seconds=max_age_seconds) if max_age_seconds else None
    )
    candidates = []
    for run in runs or []:
        if run.get("status") not in ("queued", "in_progress"):
            continue
        created_at = _parse_github_timestamp(run.get("created_at"))
        if created_at is None:
            continue
        if cutoff is not None and created_at < cutoff:
            continue
        candidates.append(run)
    if not candidates:
        return None
    # GitHub returns most-recent-first, but sort defensively by created_at
    # (ISO-8601 sorts lexically) so a re-run / dispatch can't surface an older run.
    candidates.sort(
        key=lambda r: _parse_github_timestamp(r.get("created_at")) or datetime.min,
        reverse=True,
    )
    run = candidates[0]
    return {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "html_url": run.get("html_url", ""),
        "head_sha": run.get("head_sha") or "",
        "created_at": run.get("created_at") or "",
    }


async def rerun_failed_jobs(token: str, owner: str, repo: str, run_id: int) -> bool:
    """Re-run only the failed jobs of a workflow run (GitHub Actions API).

    Used to absorb transient setup-phase infra failures (container/runner init)
    without spawning a fix task. Returns True when GitHub accepts the request
    (HTTP 201/202); False on any non-accept or error (caller falls back to the
    normal fix path).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = await _github_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
            headers,
            timeout=10.0,
        )
        if resp.status_code in (201, 202):
            return True
        logger.warning(
            f"[CI] rerun-failed-jobs for run {run_id} returned "
            f"{resp.status_code}: {resp.text[:200]}"
        )
        return False
    except Exception:  # noqa: BLE001
        logger.exception(f"[CI] rerun-failed-jobs request failed for run {run_id}")
        return False


# ─── Short-lived dedup cache for check-runs lookups ─────────────────────────
# The CI-wait loop in the orchestrator and the check_run webhook handler both
# fetch check-runs per task that's watching a PR.  When N sibling tasks share
# the same head_sha (a rollup with N children is the common case), each task
# was making its own API call — 20 children on one PR = 20 duplicate calls per
# poll interval.  A 30s TTL cache collapses siblings into one call without
# adding any cross-cycle staleness risk beyond the poll interval itself.
_CHECK_RUNS_CACHE_TTL_S = 30
_CHECK_RUNS_CACHE_MAX = 500  # entries; cheap eviction when oversized
_check_runs_cache: dict[tuple[str, str, str, bool, bool], tuple[dict, float]] = {}


def invalidate_check_runs_cache(owner: str, repo: str, head_sha: str) -> None:
    """Drop the cached check-runs result for a SHA — call when the caller
    observes a transition it needs to see immediately (e.g. a webhook arrived
    indicating a new run started).  Safe to call when the key isn't present.

    Drops EVERY variant cached for the SHA (lenient/strict x with/without
    failure logs) — a key dimension added later must never leave a stale entry
    behind an invalidation."""
    for key in [
        k
        for k in _check_runs_cache
        if k[0] == owner and k[1] == repo and k[2] == head_sha
    ]:
        _check_runs_cache.pop(key, None)


async def get_check_runs_cached(
    token: str,
    owner: str,
    repo: str,
    head_sha: str,
    *,
    project_id: str | None = None,
    ttl: float = _CHECK_RUNS_CACHE_TTL_S,
    treat_missing_as_success: bool = True,
    include_failed_logs: bool = True,
) -> dict:
    """Cached wrapper around ``get_check_runs``.

    TTL is short (30s) so a fresh webhook or a status change is picked up on
    the next orchestrator cycle — the cache exists purely to deduplicate
    simultaneous fan-out from siblings watching the same PR, not to mask
    status changes over time.

    ``treat_missing_as_success`` is forwarded to ``get_check_runs`` (default
    True preserves the fail-open behaviour) and is part of the cache key so the
    lenient and strict interpretations never serve each other's result.

    ``include_failed_logs`` is likewise forwarded AND part of the cache key: a
    logs-free result (cheap fetch, ``failed_logs=""``) must never be served to a
    caller that needs the failure detail to drive the fix loop.
    """
    import app.shared_state as _ss
    from app import github as _gh

    key = (owner, repo, head_sha, treat_missing_as_success, include_failed_logs)
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
        include_failed_logs=include_failed_logs,
    )
    # Do NOT cache API-error results — ``get_check_runs`` tags them with
    # ``_from_api_error=True`` when a non-200 response was received.  Caching
    # them would poison the 30 s dedup window with a fake 'pending' verdict
    # during GitHub 5xx / rate-limit storms, delaying CI-completion detection
    # for every task watching that SHA.  The next call will retry the real
    # lookup instead of serving a stale fake-pending.
    if result.get("_from_api_error"):
        return result
    _check_runs_cache[key] = (result, now)
    # Lazy eviction: when the dict gets oversized, drop anything older than
    # 2x TTL.  Cheap, avoids unbounded growth across long-lived processes.
    if len(_check_runs_cache) > _CHECK_RUNS_CACHE_MAX:
        cutoff = now - (ttl * 2)
        for k in [k for k, (_, ts) in _check_runs_cache.items() if ts < cutoff]:
            _check_runs_cache.pop(k, None)
    return result
