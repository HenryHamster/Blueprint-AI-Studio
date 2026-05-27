"""Qualifier finalize — phase 2 of the automain → main promotion gate.

Owns ``_finalize_qualification`` (the collect-and-decide step that runs once
the qualifier children have terminated) and the ``_BLOCKER_LABELS`` mapping
used to render its operator-notification messages.

The function is invoked by ``_finalize_locked_version`` in
``app.release_qualifier`` under that module's dedicated session + row lock;
this module owns the decision pipeline (re-resolve CI → read artifacts →
compute composite → record verdict → promote / dry-run / not-promoted) but
does NOT own the session, the lock, or the post-promotion reconciliation
hook — those stay in the coordinator (``_finalize_in_dedicated_session``).

Test-patch compatibility: several symbols that ``_finalize_qualification``
calls (``promote``, ``_resolve_ci_at_finalize``, ``_handle_infra_failure``,
``_load_policy``, ``_latest_artifact_logs``, ``_record_qualification_verdict``,
``_notify_operator``, ``_score_audit``, ``_extract_audit_findings``,
``_score_smoketest``, ``compute_composite``, ``_fmt_sub_scores``) are
canonically patched by tests via ``app.release_qualifier.<name>``. The body
therefore resolves them through a runtime accessor for the
``app.release_qualifier`` module (same pattern as
``release_qualifier_sweep._release_qualifier_module``) so test patches on
``rq.*`` continue to apply post-extraction.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import release_version
from app.failure_classification import RATE_LIMIT, classify_failure
from app.models import (
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)
from app.release_scoring import _compute_audit_reconciliation
from app.shared_state import (
    get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Maximum number of times a RATE_LIMIT-classified release_audit task will be
# re-spawned per qualification run before falling through to _handle_infra_failure.
_MAX_AUDIT_RL_RESPAWNS = 2


def _enrich_infra_error(base_label: str, task: Task | None) -> str:
    """Append agent_failure_category_raw to base_label for FAILED tasks.

    Only enriches FAILED tasks — CANCELLED tasks carry no failure_category.
    Returns base_label unchanged when task is None, CANCELLED, or has no
    agent_failure_category_raw recorded in its context.
    """
    if task is None or task.status != TaskStatus.FAILED:
        return base_label
    raw = (task.context or {}).get("agent_failure_category_raw", "")
    if not raw:
        return base_label
    sanitized = "".join(
        c if c.isalnum() or c == "_" else "_" for c in str(raw).strip().lower()
    )
    if not sanitized:
        return base_label
    return f"{base_label}_{sanitized}"


# Human-readable rendering of the compute_composite() hard-blocker codes.
_BLOCKER_LABELS: dict[str, str] = {
    "ci_not_green": "CI is not green on the candidate",
    "ci_unresolved": "CI state could not be evaluated (pending, no check-runs, or GitHub I/O degraded)",
    "smoketest_startup_failed": "the smoketest startup suite failed",
    "audit_missing": "the release audit produced no usable report",
    "test_file_deleted": "the diff deleted a test file",
}


def _release_qualifier_module():
    """Lazy accessor for app.release_qualifier — breaks the circular import.

    release_qualifier_finalize is imported BY release_qualifier (re-export);
    importing release_qualifier at module level would create a circular import
    before the parent module finishes initialising.  Deferring to call time
    (when both modules are fully loaded) is the same pattern used in
    release_qualifier_sweep._release_qualifier_module.

    Routing test-patched symbols (``promote``, ``_resolve_ci_at_finalize``,
    ``_handle_infra_failure``, etc.) through this accessor preserves the
    monkeypatch contract: tests patch ``app.release_qualifier.<name>`` and the
    finalize body reads the live attribute, so the patch still applies.
    """
    from app import release_qualifier as rq

    return rq


async def _finalize_qualification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> dict[str, Any]:
    """Finalize qualification once the qualifier children have terminated.

    Invoked by the Phase 3 batch-3 completion handlers (not wired here) after
    the release_audit child — and the qualify-smoketest child, when the
    smoketest subsystem was enabled for this run (audit H3) — for ``version``
    have reached a terminal state.

    Re-resolves CI for the candidate commit (``_resolve_ci_at_finalize``)
    rather than trusting the boolean frozen at spawn, reads the
    RELEASE_AUDIT_REPORT and (when the smoketest subsystem was
    enabled) SMOKETEST_RESULT artifacts, computes the composite, then:
      * promote == True  and promotion_enabled ⇒ ``promote()``.
      * promote == True  but promotion_enabled is False ⇒ dry-run: log the
        composite, record it, leave the version open.
      * promote == False ⇒ record the blocker, keep the version open, set
        ``phase = "stabilize"``.

    Audit H3: when the qualifying run recorded ``smoketest_enabled == False``
    the smoketest dimension is dropped — no SMOKETEST_RESULT is read and
    ``compute_composite`` renormalizes the audit + test-erosion weights with no
    smoketest hard blocker.

    Runs on whatever ``session`` it is given and does NOT commit it (the
    non-promote paths just mutate ``version.last_qualification``); the promote
    path delegates to ``promote()``, which issues its own intermediate commit.
    The caller (``_finalize_in_dedicated_session``) commits the dedicated
    session afterwards.  Returns a result dict.
    """
    rq = _release_qualifier_module()

    qual = dict(version.last_qualification or {})
    candidate_sha = qual.get("candidate_sha")
    audit_task_id = qual.get("audit_task_id")
    smoketest_task_id = qual.get("smoketest_task_id")
    # Audit H3: whether the qualifying run had the smoketest subsystem enabled.
    # Read from the recorded qualification (NOT live runtime_config) — the
    # config could have flipped between spawn and finalize, and the composite
    # must match the children that actually ran.  Legacy records that predate
    # the flag default to True (the old always-spawn behaviour).
    smoketest_enabled = bool(qual.get("smoketest_enabled", True))

    # Re-resolve CI for the candidate commit NOW rather than reusing the
    # boolean frozen at spawn — a candidate whose checks were merely pending
    # when run_qualification ran recorded ci_green=False, and trusting that
    # stale value would re-block the same commit forever.  Re-resolution lets a
    # now-completed green build pass and keeps a still-pending build not-green
    # without a permanent dead-end.  Graceful: a credential / GitHub failure
    # degrades to not-green for this attempt (never crashes the finalize).
    ci_green, ci_resolution = await rq._resolve_ci_at_finalize(
        session, version, candidate_sha
    )

    policy = rq._load_policy()

    # --- Infra-failure detection: check required child terminal statuses ------
    # A FAILED or CANCELLED required child means the qualifier pipeline itself
    # broke (agent VM crash, timeout, clone failure) — NOT a quality finding.
    # Route these to _handle_infra_failure BEFORE reading artifacts so the
    # quality-verdict path only sees COMPLETED children.  A COMPLETED child
    # whose artifact is absent or unparseable is also an infrastructure failure
    # (the agent ran but emitted no usable output) — checked after artifact
    # reading below.
    _audit_task_obj: Task | None = None
    if audit_task_id:
        try:
            _audit_task_obj = await session.get(Task, uuid.UUID(str(audit_task_id)))
            if _audit_task_obj is not None and _audit_task_obj.status in (
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                # A RATE_LIMIT-classified FAILED audit is a transient account-credit
                # failure, not a pipeline infrastructure failure.  Re-spawn the audit
                # child with the same candidate_sha so the same snapshot is scored,
                # and record the respawn count in last_qualification so the cap is
                # visible to the operator.  Non-transient FAILED/CANCELLED tasks and
                # exhausted respawns fall through to _handle_infra_failure as before.
                rl_respawn_count = qual.get("audit_rl_respawn_count", 0)
                if (
                    _audit_task_obj.status == TaskStatus.FAILED
                    and classify_failure(_audit_task_obj) == RATE_LIMIT
                    and rl_respawn_count < _MAX_AUDIT_RL_RESPAWNS
                ):
                    fp = _audit_task_obj.first_payload or {}
                    _project = await session.get(Project, version.project_id)
                    _integration_branch = get_project_integration_branch(_project)
                    new_base_branch = fp.get("base_branch", "") or _integration_branch
                    new_diff_range = (
                        fp.get("diff_range", "")
                        or f"{_integration_branch}...{candidate_sha}"
                    )
                    new_audit_task = await rq._spawn_release_audit_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=new_base_branch,
                        diff_range=new_diff_range,
                        candidate_sha=candidate_sha,
                    )
                    new_count = rl_respawn_count + 1
                    updated_qual = {
                        **qual,
                        "audit_task_id": str(new_audit_task.id),
                        "audit_rl_respawn_count": new_count,
                    }
                    version.last_qualification = updated_qual
                    flag_modified(version, "last_qualification")
                    session.add(version)
                    await session.flush()
                    logger.info(
                        "[RELEASE_QUALIFIER] v%d audit task %s was rate-limited; "
                        "re-spawned as %s (respawn %d/%d)",
                        version.version_number,
                        audit_task_id,
                        new_audit_task.id,
                        new_count,
                        _MAX_AUDIT_RL_RESPAWNS,
                    )
                    return {
                        "status": "audit_rate_limit_respawned",
                        "respawn_count": new_count,
                    }
                infra_label = _enrich_infra_error(
                    f"audit_task_{_audit_task_obj.status.lower()}",
                    _audit_task_obj,
                )
                if (
                    _audit_task_obj.status == TaskStatus.FAILED
                    and classify_failure(_audit_task_obj) == RATE_LIMIT
                    and rl_respawn_count >= _MAX_AUDIT_RL_RESPAWNS
                ):
                    infra_label = "audit_rate_limit_respawn_cap_exceeded"
                return await rq._handle_infra_failure(
                    session,
                    version,
                    qual,
                    infra_label,
                )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid audit_task_id %s on version v%d "
                "(skipping infra-failure check)",
                audit_task_id,
                version.version_number,
            )

    if smoketest_enabled and smoketest_task_id:
        try:
            _smoke_task_obj = await session.get(Task, uuid.UUID(str(smoketest_task_id)))
            if _smoke_task_obj is not None and _smoke_task_obj.status in (
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                return await rq._handle_infra_failure(
                    session,
                    version,
                    qual,
                    _enrich_infra_error(
                        f"smoketest_task_{_smoke_task_obj.status.lower()}",
                        _smoke_task_obj,
                    ),
                )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid smoketest_task_id %s on version v%d "
                "(skipping infra-failure check)",
                smoketest_task_id,
                version.version_number,
            )

    # --- Read the release_audit report -------------------------------------
    audit_report: dict[str, Any] | None = None
    if audit_task_id:
        try:
            audit_report = await rq._latest_artifact_logs(
                session,
                uuid.UUID(str(audit_task_id)),
                ArtifactType.RELEASE_AUDIT_REPORT,
            )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid audit_task_id %s on version v%d",
                audit_task_id,
                version.version_number,
            )
    # Defense-in-depth: a COMPLETED audit task whose RELEASE_AUDIT_REPORT
    # artifact is absent or unparseable is an infrastructure failure — the
    # agent ran but produced no usable output.  Route to _handle_infra_failure
    # so the operator notification is "release_infra_failure" (not
    # "release_blocked / audit_missing"), and no QualificationVerdict row is
    # written.
    if (
        audit_report is None
        and audit_task_id
        and _audit_task_obj is not None
        and _audit_task_obj.status == TaskStatus.COMPLETED
    ):
        return await rq._handle_infra_failure(
            session, version, qual, "audit_report_missing"
        )

    audit_score, test_erosion_score = rq._score_audit(audit_report)
    # Observability: record model-reported vs findings-derived score delta and
    # count of findings excluded for missing/short verification_note.
    audit_reconciliation = _compute_audit_reconciliation(audit_report)
    # Carry the findings (not just the score) into last_qualification so the
    # strategic agent can target them — otherwise the audit sub-score, usually
    # the binding constraint, is opaque to the loop.
    audit_findings = rq._extract_audit_findings(audit_report)
    audit_missing_reason = rq._audit_missing_reason(audit_report)
    if audit_missing_reason == "parseable_but_unscoreable":
        return await rq._handle_infra_failure(
            session, version, qual, "audit_report_parseable_but_unscoreable"
        )
    # An empty artifact dict has no scoring signal at all — _score_audit()
    # returns a silent clean 1.0 because no deductions ever apply.
    if isinstance(audit_report, dict) and not audit_report:
        return await rq._handle_infra_failure(
            session, version, qual, "audit_report_empty_dict"
        )
    # A "findings" key whose value is not a list is a malformed artifact shape.
    # _score_audit() falls through to the "issues" fallback; if that is also
    # absent or not a list, derived=1.0 with no deductions — treat as infra_failure.
    if isinstance(audit_report, dict):
        _findings_raw = audit_report.get("findings")
        if _findings_raw is not None and not isinstance(_findings_raw, list):
            if not isinstance(audit_report.get("issues"), list):
                return await rq._handle_infra_failure(
                    session, version, qual, "audit_report_findings_wrong_type"
                )

    # --- Read the smoketest result -----------------------------------------
    # Audit H3: only when the smoketest subsystem was enabled — a disabled run
    # spawned no qualifier-smoketest child, so there is no SMOKETEST_RESULT to
    # read and smoketest_score stays None (compute_composite drops the
    # dimension rather than hard-blocking on it).
    smoketest_result: dict[str, Any] | None = None
    if smoketest_enabled and smoketest_task_id:
        try:
            smoketest_result = await rq._latest_artifact_logs(
                session,
                uuid.UUID(str(smoketest_task_id)),
                ArtifactType.SMOKETEST_RESULT,
            )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid smoketest_task_id %s on version v%d",
                smoketest_task_id,
                version.version_number,
            )
    if (
        smoketest_enabled
        and smoketest_task_id
        and _smoke_task_obj is not None
        and _smoke_task_obj.status == TaskStatus.COMPLETED
        and smoketest_result is None
    ):
        return await rq._handle_infra_failure(
            session, version, qual, "smoketest_report_missing"
        )
    smoketest_score = rq._score_smoketest(smoketest_result)

    # --- Compute the composite ---------------------------------------------
    composite = rq.compute_composite(
        ci_green=ci_green,
        audit_score=audit_score,
        test_erosion=test_erosion_score,
        smoketest_score=smoketest_score,
        policy=policy,
        smoketest_enabled=smoketest_enabled,
        ci_state=ci_resolution.get("state", rq._CI_RED),
    )

    finalized_at = _utcnow().isoformat()
    base_record: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "ci_resolution": ci_resolution,
        "smoketest_enabled": smoketest_enabled,
        "confidence": composite["confidence"],
        "sub_scores": composite["sub_scores"],
        "blocker": composite["blocker"],
        "threshold": policy.get("threshold"),
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": audit_task_id,
        "smoketest_task_id": smoketest_task_id,
        "audit_findings": audit_findings,
        "audit_missing_reason": audit_missing_reason,
        "audit_reconciliation": audit_reconciliation,
    }

    promotion_enabled = bool(_runtime_config.get("promotion_enabled"))

    logger.info(
        "[RELEASE_QUALIFIER] v%d composite=%.4f promote=%s blocker=%s "
        "sub_scores=%s promotion_enabled=%s",
        version.version_number,
        composite["confidence"],
        composite["promote"],
        composite["blocker"],
        composite["sub_scores"],
        promotion_enabled,
    )

    # --- Decide -------------------------------------------------------------
    if not composite["promote"]:
        # Below threshold (or a hard blocker fired): keep the version open and
        # send it back to stabilize so the next strategic cycle targets the
        # blocker.  A failed qualifier only ever blocks — which is safe.
        record = {**base_record, "status": "not_promoted"}
        version.last_qualification = record
        flag_modified(version, "last_qualification")
        reason = (
            f"qualification v{version.version_number}: "
            f"confidence {composite['confidence']:.2f} < "
            f"{policy.get('threshold')}"
        )
        if composite["blocker"]:
            reason = (
                f"qualification v{version.version_number} blocked: "
                f"{composite['blocker']}"
            )
        release_version.set_version_phase(version, "stabilize", reasoning=reason)
        session.add(version)
        await rq._record_qualification_verdict(
            session,
            version,
            record,
            terminal_status="not_promoted",
            would_promote=False,
            promotion_enabled=promotion_enabled,
        )
        scores_str = rq._fmt_sub_scores(
            composite["sub_scores"], smoketest_enabled=smoketest_enabled
        )
        if composite["blocker"]:
            blocker_label = _BLOCKER_LABELS.get(
                composite["blocker"], composite["blocker"]
            )
            notify_event = "release_blocked"
            notify_summary = (
                f"v{version.version_number} blocked from promotion — "
                f"{blocker_label}. Not promoted; staying in stabilize."
            )
        else:
            notify_event = "release_below_threshold"
            notify_summary = (
                f"v{version.version_number} qualified at confidence "
                f"{composite['confidence']:.2f}, below the "
                f"{policy.get('threshold')} promotion bar — not promoted, "
                f"staying in stabilize. Sub-scores: {scores_str}."
            )
        await rq._notify_operator(
            session,
            version,
            notify_event,
            notify_summary,
            extra={
                "confidence": composite["confidence"],
                "threshold": policy.get("threshold"),
                "sub_scores": composite["sub_scores"],
                "blocker": composite["blocker"],
            },
        )
        return {
            "status": "not_promoted",
            "confidence": composite["confidence"],
            "blocker": composite["blocker"],
        }

    if not promotion_enabled:
        # Dry-run: the candidate would promote, but Phase 3 ships dark.  Record
        # the composite and leave the version open — interim-manual promotion
        # continues elsewhere.
        record = {**base_record, "status": "dry_run", "would_promote": True}
        version.last_qualification = record
        flag_modified(version, "last_qualification")
        session.add(version)
        await rq._record_qualification_verdict(
            session,
            version,
            record,
            terminal_status="dry_run",
            would_promote=True,
            promotion_enabled=False,
        )
        logger.info(
            "[RELEASE_QUALIFIER] v%d would promote (confidence=%.4f) but "
            "promotion_enabled=False — dry-run, not promoting",
            version.version_number,
            composite["confidence"],
        )
        await rq._notify_operator(
            session,
            version,
            "release_dry_run",
            (
                f"v{version.version_number} would promote (confidence "
                f"{composite['confidence']:.2f}) but promotion is disabled — "
                f"dry-run only, main not advanced."
            ),
            extra={
                "confidence": composite["confidence"],
                "threshold": policy.get("threshold"),
                "sub_scores": composite["sub_scores"],
            },
        )
        return {
            "status": "dry_run",
            "confidence": composite["confidence"],
            "would_promote": True,
        }

    # --- Promote ------------------------------------------------------------
    if not candidate_sha:
        # Should not happen — run_qualification always records it — but guard
        # so a missing SHA degrades to stabilize rather than a crash.
        record = {**base_record, "status": "error", "error": "missing_candidate_sha"}
        version.last_qualification = record
        flag_modified(version, "last_qualification")
        release_version.set_version_phase(
            version,
            "stabilize",
            reasoning=f"qualification v{version.version_number}: missing SHA",
        )
        session.add(version)
        await rq._notify_operator(
            session,
            version,
            "release_error",
            (
                f"v{version.version_number} qualification could not complete "
                f"— candidate SHA missing. Returned to stabilize."
            ),
            extra={"error": "missing_candidate_sha"},
        )
        return {"status": "error", "error": "missing_candidate_sha"}

    # Delegate to promote().  Credentials are NOT pre-resolved here on purpose
    # (concern 2): credential resolution is GitHub I/O, and at this point the
    # dedicated session still holds the ReleaseVersion FOR UPDATE lock.
    # promote() releases that lock via its intermediate commit and then resolves
    # the token unlocked — so no GitHub round-trip ever runs under the row lock.
    return await rq.promote(
        session,
        version,
        candidate_sha,
        composite=composite,
        base_record=base_record,
    )
