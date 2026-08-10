"""Recurring detection of approved pull requests that never reached main.

Review approval is a workflow fact, not proof that a commit landed.  This
module keeps the detector on the reaper's existing recurring surface.  The
deployed path uses the authenticated GitHub API because the backend image has
no checkout; an explicit ``repo_root`` remains available for deterministic
local fixtures.  Every evidence path is tri-state: unavailable or incomplete
evidence is ``unknown`` rather than ``clean``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.branch_resolution import get_project_integration_branch
from app.landed_signal import (
    LANDED_MERGE_SHA_KEY,
    LANDED_ON_INTEGRATION_KEY,
    LANDED_SIGNAL_KEY,
    STRANDED_LANDING_REASON_KEY,
)
from app.models import Alert, AlertSeverity, Project, ReleaseVersion, Task
from app.time_utils import _is_cadence_due, _strip_tz_assume_utc, _utcnow

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 5
_APPROVED_PR_BATCH_SIZE = 100
_MAX_CYCLE_IDS = 8
_STRANDING_SCAN_INTERVAL_SECONDS = 15 * 60
_last_stranding_scan: datetime | None = None
_OBSERVATION_KEY = "approved_pr_stranding"
_EVENT_TYPE = "release.approved_pr_stranded"
_ALERT_TITLE = "Approved pull request has not reached the integration branch"

EvidenceState = Literal["landed", "stranded", "unknown"]
Mergeability = Literal["clean", "conflicted", "not_applicable", "unknown"]
ReachabilitySignal = Literal["merge_sha", "head_sha", "neither"]


@dataclass(frozen=True)
class ApprovedPrEvidence:
    """The authoritative git evidence for one approved review target."""

    state: EvidenceState
    mergeability: Mergeability
    integration_head_sha: str | None = None
    merge_base_sha: str | None = None
    reason: str | None = None
    reachability_signal: ReachabilitySignal = "neither"
    checked_commit_sha: str | None = None
    merge_commit_sha: str | None = None


@dataclass(frozen=True)
class _ApprovedPrCandidate:
    task: Task
    project: Project
    pr_number: int | None
    base_sha: str | None
    head_sha: str | None
    commit_sha: str | None
    target_valid: bool
    target_reason: str | None = None


def _git(args: list[str], *, repo_root: str) -> subprocess.CompletedProcess[str] | None:
    """Run a bounded git command, returning None when evidence is unavailable."""
    if not repo_root or not os.path.isdir(repo_root):
        return None
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={repo_root}", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("[APPROVED_PR] git evidence unavailable in %s", repo_root)
        return None


def _resolve_integration_head(branch: str, *, repo_root: str) -> str | None:
    """Resolve a local integration branch or its origin-tracking ref."""
    if not branch or any(char.isspace() for char in branch) or ".." in branch:
        return None
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        result = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo_root=repo_root)
        if result is not None and result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    return None


def inspect_approved_pr(
    *,
    base_sha: str | None,
    head_sha: str | None,
    merge_commit_sha: str | None = None,
    integration_branch: str,
    repo_root: str,
) -> ApprovedPrEvidence:
    """Classify a review target using merge-SHA-first ancestry evidence.

    Squash merges create a new integration-branch commit, so the PR head is
    not necessarily reachable even when the PR landed.  Check the immutable
    merge commit first and use the review head only when no merge commit proves
    reachability.  A non-ancestor target's three-way merge tree distinguishes
    clean work from a genuine conflict without requiring GitHub CLI auth.
    """
    if not base_sha or not (head_sha or merge_commit_sha):
        return ApprovedPrEvidence(
            "unknown", "unknown", reason="review target lacks base_sha and commit SHA"
        )
    integration_head_sha = _resolve_integration_head(
        integration_branch, repo_root=repo_root
    )
    if not integration_head_sha:
        return ApprovedPrEvidence(
            "unknown",
            "unknown",
            reason=f"integration branch {integration_branch!r} is unavailable locally",
        )

    ancestry_errors: list[str] = []
    attempted: list[str] = []
    ancestry_candidates: tuple[tuple[ReachabilitySignal, str | None], ...] = (
        ("merge_sha", merge_commit_sha),
        ("head_sha", head_sha),
    )
    for signal, commit_sha in ancestry_candidates:
        if not commit_sha or commit_sha in attempted:
            continue
        attempted.append(commit_sha)
        ancestry = _git(
            ["merge-base", "--is-ancestor", commit_sha, integration_head_sha],
            repo_root=repo_root,
        )
        if ancestry is None:
            ancestry_errors.append(f"{signal}: git ancestry check failed")
            continue
        if ancestry.returncode == 0:
            return ApprovedPrEvidence(
                "landed",
                "not_applicable",
                integration_head_sha=integration_head_sha,
                reachability_signal=signal,
                checked_commit_sha=commit_sha,
                merge_commit_sha=merge_commit_sha,
            )
        if ancestry.returncode != 1:
            ancestry_errors.append(
                f"{signal}: git ancestry check exited {ancestry.returncode}"
            )

    if not attempted or ancestry_errors:
        return ApprovedPrEvidence(
            "unknown",
            "unknown",
            integration_head_sha,
            reason="; ".join(ancestry_errors) or "git ancestry check failed",
            checked_commit_sha=merge_commit_sha or head_sha,
            merge_commit_sha=merge_commit_sha,
        )

    mergeability_sha = head_sha or merge_commit_sha
    assert mergeability_sha is not None

    # Mergeability is evaluated against the current integration tip, not the
    # stale base SHA captured when the review was opened.  That is what makes a
    # clean strand distinguishable from a PR that now conflicts with main.
    merge_base = _git(
        ["merge-base", integration_head_sha, mergeability_sha],
        repo_root=repo_root,
    )
    if merge_base is None or merge_base.returncode != 0 or not merge_base.stdout.strip():
        return ApprovedPrEvidence(
            "unknown",
            "unknown",
            integration_head_sha,
            reason="could not resolve the review target merge base",
            checked_commit_sha=mergeability_sha,
            merge_commit_sha=merge_commit_sha,
        )
    merge_base_sha = merge_base.stdout.strip()
    merge_tree = _git(
        ["merge-tree", "--write-tree", integration_head_sha, mergeability_sha],
        repo_root=repo_root,
    )
    if merge_tree is None:
        return ApprovedPrEvidence(
            "unknown",
            "unknown",
            integration_head_sha,
            merge_base_sha,
            "git merge-tree check failed",
            checked_commit_sha=mergeability_sha,
            merge_commit_sha=merge_commit_sha,
        )
    if (
        merge_tree.returncode != 0
        and "unknown rev --write-tree" in merge_tree.stderr
    ):
        # Git 2.34 (still present on some self-hosted workers) has the older
        # three-revision interface but not --write-tree.  Its exit status has
        # the same conflict contract, while the output remains intentionally
        # ignored so conflict-marker text in a clean file cannot false-positive.
        merge_tree = _git(
            ["merge-tree", merge_base_sha, integration_head_sha, mergeability_sha],
            repo_root=repo_root,
        )
        if merge_tree is None:
            return ApprovedPrEvidence(
                "unknown",
                "unknown",
                integration_head_sha,
                merge_base_sha,
                "legacy git merge-tree check failed",
                checked_commit_sha=mergeability_sha,
                merge_commit_sha=merge_commit_sha,
            )
        if any(
            marker in f"{merge_tree.stdout}\n{merge_tree.stderr}"
            for marker in ("changed in both", "removed in both", "added in both", "CONFLICT")
        ):
            merge_tree = subprocess.CompletedProcess(
                merge_tree.args,
                1,
                merge_tree.stdout,
                merge_tree.stderr,
            )
    # The --write-tree interface uses its exit status as the conflict signal.
    # Do not scan stdout/stderr: that stream includes merged file content,
    # which may legitimately document conflict markers.
    if merge_tree.returncode == 1:
        return ApprovedPrEvidence(
            "stranded",
            "conflicted",
            integration_head_sha,
            merge_base_sha,
            checked_commit_sha=mergeability_sha,
            merge_commit_sha=merge_commit_sha,
        )
    if merge_tree.returncode != 0:
        return ApprovedPrEvidence(
            "unknown",
            "unknown",
            integration_head_sha,
            merge_base_sha,
            f"git merge-tree exited {merge_tree.returncode}",
            checked_commit_sha=mergeability_sha,
            merge_commit_sha=merge_commit_sha,
        )
    return ApprovedPrEvidence(
        "stranded",
        "clean",
        integration_head_sha,
        merge_base_sha,
        checked_commit_sha=merge_commit_sha or mergeability_sha,
        merge_commit_sha=merge_commit_sha,
    )


async def _inspect_approved_pr_with_github(
    *,
    session,
    project: Project,
    candidate: _ApprovedPrCandidate,
) -> ApprovedPrEvidence:
    """Classify a target through the deployed backend's GitHub API path.

    The compare endpoint is the source of merge truth: a PR approval or PR
    state is never treated as proof that the reviewed commit reached the
    integration branch.  The branch tip is fetched separately so the evidence
    and operator alert identify the exact head that was checked.
    """
    from app import github
    from app.release_promotion import _resolve_github_with_session

    try:
        resolved = await _resolve_github_with_session(session, project)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[APPROVED_PR] GitHub credentials unavailable for project %s: %s",
            project.id,
            exc,
        )
        return ApprovedPrEvidence(
            "unknown", "unknown", reason="GitHub credentials unavailable"
        )
    if resolved is None:
        return ApprovedPrEvidence(
            "unknown", "unknown", reason="GitHub repository credentials unavailable"
        )
    token, owner, repo = resolved
    project_id = str(project.id)
    branch = get_project_integration_branch(project)

    # Mergeability is descriptive only. It is deliberately fetched separately
    # from ancestry so a temporarily unavailable PR detail cannot turn a
    # definitive compare result into an unknown verdict.  A merged PR's API
    # payload is also the durable source for the squash merge SHA when the
    # merge observer did not already stamp it on the task.
    mergeability: Mergeability = "unknown"
    observed_merge_commit_sha: str | None = None
    try:
        pull = await github.get_pull_request(
            token, owner, repo, candidate.pr_number, project_id=project_id
        )
        if pull.get("mergeable") is True:
            mergeability = "clean"
        elif pull.get("mergeable") is False:
            mergeability = "conflicted"
        api_merge_commit_sha = pull.get("merge_commit_sha")
        if isinstance(api_merge_commit_sha, str) and api_merge_commit_sha.strip():
            observed_merge_commit_sha = api_merge_commit_sha.strip()[:64]
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[APPROVED_PR] PR detail unavailable for #%s: %s",
            candidate.pr_number,
            exc,
        )

    try:
        integration_head_sha = await github.get_branch_sha(
            token, owner, repo, branch, project_id=project_id, live=True
        )
        merge_commit_sha = observed_merge_commit_sha or (
            candidate.commit_sha
            if candidate.commit_sha and candidate.commit_sha != candidate.head_sha
            else None
        )
        ancestry_results: list[tuple[ReachabilitySignal, str, bool | None]] = []
        attempted_commits: set[str] = set()
        ancestry_candidates: tuple[tuple[ReachabilitySignal, str | None], ...] = (
            ("merge_sha", merge_commit_sha),
            ("head_sha", candidate.head_sha),
        )
        for signal, commit_sha in ancestry_candidates:
            if not commit_sha or commit_sha in attempted_commits:
                continue
            attempted_commits.add(commit_sha)
            reachable = await github.is_ancestor_of(
                token,
                owner,
                repo,
                commit_sha,
                branch,
                project_id=project_id,
            )
            ancestry_results.append((signal, commit_sha, reachable))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[APPROVED_PR] GitHub ancestry unavailable for PR #%s: %s",
            candidate.pr_number,
            exc,
        )
        return ApprovedPrEvidence(
            "unknown", "unknown", reason="GitHub ancestry check unavailable"
        )

    for signal, commit_sha, reachable in ancestry_results:
        if reachable is True:
            return ApprovedPrEvidence(
                "landed",
                "not_applicable",
                integration_head_sha=integration_head_sha,
                reachability_signal=signal,
                checked_commit_sha=commit_sha,
                merge_commit_sha=observed_merge_commit_sha,
            )
    if ancestry_results and all(reachable is False for _, _, reachable in ancestry_results):
        return ApprovedPrEvidence(
            "stranded",
            mergeability,
            integration_head_sha=integration_head_sha,
            reachability_signal="neither",
            checked_commit_sha=ancestry_results[0][1],
            merge_commit_sha=observed_merge_commit_sha,
            reason="GitHub compare confirms the reviewed commit is not on the integration branch",
        )
    return ApprovedPrEvidence(
        "unknown",
        "unknown",
        integration_head_sha=integration_head_sha,
        checked_commit_sha=(ancestry_results[0][1] if ancestry_results else None),
        merge_commit_sha=observed_merge_commit_sha,
        reason="GitHub compare returned no definitive ancestry result",
    )


def _candidate_from_task(task: Task, project: Project) -> _ApprovedPrCandidate | None:
    context = task.context or {}
    review_loop = context.get("review_loop") or {}
    merged_without_integration_landing = (
        context.get(LANDED_SIGNAL_KEY) is True
        and context.get(LANDED_ON_INTEGRATION_KEY) is False
    )
    if (
        review_loop.get("review_approved") is not True
        and not merged_without_integration_landing
        and not context.get(STRANDED_LANDING_REASON_KEY)
    ):
        return None
    target = review_loop.get("review_target")
    merge_commit_sha = context.get(LANDED_MERGE_SHA_KEY)
    if not isinstance(merge_commit_sha, str) or not merge_commit_sha.strip():
        merge_commit_sha = None
    merged_evidence = bool(
        merge_commit_sha
        and (
            merged_without_integration_landing
            or context.get(STRANDED_LANDING_REASON_KEY)
        )
    )
    if not isinstance(target, dict) and not merged_evidence:
        return _ApprovedPrCandidate(
            task,
            project,
            None,
            None,
            merge_commit_sha,
            merge_commit_sha,
            False,
            "approved or merged review has no review_target",
        )
    target = target if isinstance(target, dict) else {}
    pr_number = (
        target.get("pr_number")
        or review_loop.get("pr_number")
        or context.get("rollup_pr_number")
        or context.get("pr_number")
    )
    base_sha = (
        target.get("base_sha")
        or review_loop.get("base_sha")
        or context.get("base_sha")
        or ("merged-commit" if merged_evidence else None)
    )
    head_sha = target.get("head_sha") or review_loop.get("head_sha")
    commit_sha = merge_commit_sha or head_sha
    if (
        isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number <= 0
        or not isinstance(base_sha, str)
        or not base_sha.strip()
        or not isinstance(commit_sha, str)
        or not commit_sha.strip()
    ):
        return _ApprovedPrCandidate(
            task,
            project,
            pr_number if isinstance(pr_number, int) and not isinstance(pr_number, bool) else None,
            base_sha if isinstance(base_sha, str) and base_sha.strip() else None,
            head_sha if isinstance(head_sha, str) and head_sha.strip() else None,
            commit_sha if isinstance(commit_sha, str) and commit_sha.strip() else None,
            False,
            "approved review target is incomplete or malformed",
        )
    return _ApprovedPrCandidate(
        task,
        project,
        pr_number,
        base_sha.strip(),
        head_sha.strip() if isinstance(head_sha, str) and head_sha.strip() else None,
        commit_sha.strip(),
        True,
    )


def _qualification_cycle_id(version: ReleaseVersion | None) -> str | None:
    if version is None:
        return None
    active = getattr(version, "active_qualification_attempt_id", None)
    if active:
        return str(active)
    last = version.last_qualification or {}
    value = last.get("qualification_attempt_id")
    return str(value) if value else None


def _observation_for(
    candidate: _ApprovedPrCandidate,
    evidence: ApprovedPrEvidence,
    *,
    cycle_id: str | None,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    task = candidate.task
    old = (task.context or {}).get(_OBSERVATION_KEY) or {}
    identity = f"{candidate.pr_number}:{candidate.commit_sha or 'unknown'}"
    if old.get("identity") != identity:
        old = {}
    cycle_ids = list(old.get("qualification_cycle_ids") or [])
    if cycle_id and cycle_id not in cycle_ids:
        cycle_ids.append(cycle_id)
        cycle_ids = cycle_ids[-_MAX_CYCLE_IDS:]
    updated = {
        "identity": identity,
        "pr_number": candidate.pr_number,
        "base_sha": candidate.base_sha,
        "head_sha": candidate.head_sha,
        "checked_commit_sha": evidence.checked_commit_sha or candidate.commit_sha,
        "merge_commit_sha": evidence.merge_commit_sha
        or (candidate.commit_sha if candidate.commit_sha != candidate.head_sha else None),
        "reachability_signal": evidence.reachability_signal,
        "state": evidence.state,
        "mergeability": evidence.mergeability,
        "integration_branch": get_project_integration_branch(candidate.project),
        "integration_head_sha": evidence.integration_head_sha,
        "merge_base_sha": evidence.merge_base_sha,
        "reason": evidence.reason or candidate.target_reason,
        "first_observed_at": old.get("first_observed_at") or now.isoformat(),
        "last_observed_at": now.isoformat(),
        "qualification_cycle_ids": cycle_ids,
        "qualification_cycles_seen": len(cycle_ids),
    }
    changed = updated != old
    if changed:
        context = dict(task.context or {})
        if evidence.merge_commit_sha:
            context[LANDED_MERGE_SHA_KEY] = evidence.merge_commit_sha
        context[_OBSERVATION_KEY] = updated
        task.context = context
        flag_modified(task, "context")
    return updated, changed


async def sweep_approved_pr_stranding(
    session, *, repo_root: str | None = None, now: datetime | None = None
) -> dict[str, int]:
    """Scan approved targets and durable merged records for non-landed work.

    Task observations are committed before alerts.  That ordering makes the
    detector's evidence durable even if alert creation or notification later
    fails, and prevents an alert from claiming a check that was never recorded.
    """
    global _last_stranding_scan
    if repo_root is None:
        scan_now = _strip_tz_assume_utc(now or _utcnow()) or _utcnow()
        last_scan = _strip_tz_assume_utc(_last_stranding_scan)
        if not _is_cadence_due(
            last_scan, _STRANDING_SCAN_INTERVAL_SECONDS, scan_now
        ):
            return {"scanned": 0, "stranded": 0, "unknown": 0, "alerts_created": 0}
        _last_stranding_scan = scan_now

    rows = (
        await session.execute(
            select(Task, Project)
            .join(Project, Project.id == Task.project_id)
            .where(
                or_(
                    and_(
                        Task.archived.is_(False),
                        Task.context["review_loop"]["review_approved"]
                        .as_boolean()
                        .is_(True),
                    ),
                    and_(
                        Task.context[LANDED_MERGE_SHA_KEY]
                        .as_string()
                        .is_not(None),
                        or_(
                            and_(
                                Task.context[LANDED_ON_INTEGRATION_KEY]
                                .as_boolean()
                                .is_(False),
                                Task.context[LANDED_SIGNAL_KEY]
                                .as_boolean()
                                .is_(True),
                            ),
                            Task.context[STRANDED_LANDING_REASON_KEY]
                            .as_string()
                            .is_not(None),
                        ),
                    ),
                )
            )
            .order_by(Task.updated_at.asc())
            .limit(_APPROVED_PR_BATCH_SIZE)
        )
    ).all()
    if not rows:
        return {"scanned": 0, "stranded": 0, "unknown": 0, "alerts_created": 0}

    projects = {project.id: project for _, project in rows}
    versions = (
        await session.execute(
            select(ReleaseVersion)
            .where(
                ReleaseVersion.project_id.in_(projects),
                ReleaseVersion.status.in_(("open", "promoted")),
            )
            .order_by(ReleaseVersion.opened_at.desc())
        )
    ).scalars().all()
    current_versions: dict[Any, ReleaseVersion] = {}
    for version in versions:
        # A newly opened post-promotion version may not have a qualification
        # attempt yet.  In that window, use the latest promoted version's
        # persisted qualification identity instead of going silent.
        cycle_id = _qualification_cycle_id(version)
        if version.project_id not in current_versions and cycle_id:
            current_versions[version.project_id] = version

    candidates: dict[tuple[Any, int, str], _ApprovedPrCandidate] = {}
    for task, project in rows:
        candidate = _candidate_from_task(task, project)
        if candidate is None:
            continue
        candidate_key = (
            project.id,
            candidate.pr_number or 0,
            candidate.commit_sha or str(task.id),
        )
        candidates.setdefault(candidate_key, candidate)

    now = _strip_tz_assume_utc(now or _utcnow()) or _utcnow()
    observations: list[tuple[_ApprovedPrCandidate, dict[str, Any]]] = []
    changed_tasks = False
    counts = {"scanned": len(candidates), "stranded": 0, "unknown": 0, "alerts_created": 0}
    for candidate in candidates.values():
        branch = get_project_integration_branch(candidate.project)
        if not candidate.target_valid:
            evidence = ApprovedPrEvidence(
                "unknown", "unknown", reason=candidate.target_reason
            )
        elif repo_root is None:
            evidence = await _inspect_approved_pr_with_github(
                session=session, project=candidate.project, candidate=candidate
            )
        else:
            evidence = await asyncio.to_thread(
                inspect_approved_pr,
                base_sha=candidate.base_sha,
                head_sha=candidate.head_sha or candidate.commit_sha,
                merge_commit_sha=(
                    candidate.commit_sha
                    if candidate.commit_sha and candidate.commit_sha != candidate.head_sha
                    else None
                ),
                integration_branch=branch,
                repo_root=repo_root,
            )
        if evidence.state == "stranded":
            counts["stranded"] += 1
        elif evidence.state == "unknown":
            counts["unknown"] += 1
        observation, changed = _observation_for(
            candidate,
            evidence,
            cycle_id=_qualification_cycle_id(current_versions.get(candidate.project.id)),
            now=now,
        )
        if changed:
            session.add(candidate.task)
            changed_tasks = True
        observations.append((candidate, observation))

    # Publish the evidence before any alert row is created.
    if changed_tasks:
        await session.commit()

    all_alert_keys = {
        f"approved_pr_stranding:{candidate.project.id}:{candidate.pr_number}:{candidate.commit_sha}"
        for candidate, observation in observations
        if candidate.pr_number is not None and candidate.commit_sha
    }
    alert_keys = {
        f"approved_pr_stranding:{candidate.project.id}:{candidate.pr_number}:{candidate.commit_sha}"
        for candidate, observation in observations
        if observation.get("state") == "stranded"
        and observation.get("qualification_cycles_seen", 0) >= 2
    }
    existing = {}
    if all_alert_keys:
        existing_rows = (
            await session.execute(
                select(Alert).where(
                    Alert.dedupe_key.in_(all_alert_keys), Alert.dismissed_at.is_(None)
                )
            )
        ).scalars().all()
        existing = {alert.dedupe_key: alert for alert in existing_rows}

    for candidate, observation in observations:
        alert_key = (
            f"approved_pr_stranding:{candidate.project.id}:{candidate.pr_number}:"
            f"{candidate.commit_sha}"
        )
        alert = existing.get(alert_key)
        if observation.get("state") == "landed" and alert is not None:
            alert.dismissed_at = now
            alert.last_seen_at = now
            continue
        if (
            observation.get("state") != "stranded"
            or observation.get("qualification_cycles_seen", 0) < 2
        ):
            continue
        message = (
            f"PR #{candidate.pr_number} has a recorded merge/review target, but checked commit "
            f"{observation.get('checked_commit_sha') or candidate.commit_sha} is not an ancestor of "
            f"{observation.get('integration_branch')} HEAD "
            f"{observation.get('integration_head_sha') or 'unknown'}. "
            f"Reachability signal: {observation.get('reachability_signal', 'neither')}. "
            f"Mergeability classification: {observation.get('mergeability')}. "
            "Review approval is not treated as proof of landing."
        )
        if alert is None:
            session.add(
                Alert(
                    project_id=candidate.project.id,
                    severity=AlertSeverity.WARNING,
                    title=_ALERT_TITLE,
                    message=message[:2000],
                    event_type=_EVENT_TYPE,
                    source="approved_pr_stranding_detector",
                    dedupe_key=alert_key,
                    last_seen_at=now,
                    task_id=candidate.task.id,
                )
            )
            counts["alerts_created"] += 1
        else:
            alert.occurrence_count = (alert.occurrence_count or 0) + 1
            alert.last_seen_at = now
            alert.message = message[:2000]
    if alert_keys or any(observation.get("state") == "landed" for _, observation in observations):
        await session.commit()
    return counts


__all__ = [
    "ApprovedPrEvidence",
    "inspect_approved_pr",
    "sweep_approved_pr_stranding",
]
