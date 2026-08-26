"""Auto-qualification trigger executor for the release qualifier sweep.

Extracted from ``release_qualifier_sweep`` -- a chronic size offender whose cap
is decomposed, never bumped (see
``test_release_qualifier_sweep_decomposition_resolves_size_breach``).

The sweep's other legs FINISH qualifications: finalize wedged ones, re-drive
stranded ones, reconcile out-of-band promotions.  This one STARTS a
qualification.  It resolves the sanctioned trigger for one open version through
``qualification_triggers.resolve_qualification_trigger``, applies the
executor-ownership / CI-compliance / live-audit guards, and calls
``run_qualification``.  That opposite direction, plus the single call site in
the sweep orchestrator, is the seam.

The public surface is UNCHANGED: ``release_qualifier_sweep`` re-exports
``_fire_auto_triggered_qualification``, so it stays importable and
test-patchable at its original path (``test_promotion_read_surface`` reads it
back with ``inspect.getsource`` from there).
"""

from __future__ import annotations

import logging
from typing import Any

from app import release_version
from app.database import AsyncSessionLocal
from app.models import Project
from app.release_qualifier_debounce import (
    compute_automain_head_quiet_seconds,
    find_live_qualifier_audit_task_id,
)
from app.release_qualifier_redrive import _record_discarded_trigger_routing
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# One sweep leg -- the auto-qualification trigger executor -- and nothing else.
# The cap is policy headroom over the extracted size.  The routing table itself
# stays in ``qualification_triggers`` (a second private copy here is a defect
# the executor has already had once), so there is nothing this module should
# grow to hold.
_MODULE_SIZE_GOVERNANCE_CAP: int = 450


async def _fire_auto_triggered_qualification(
    version_id: Any,
    *,
    session_factory=None,
    pre_sweep_status: str | None = None,
) -> bool:
    """Resolve and execute one sanctioned automatic qualification decision.

    The resolver always runs before executor-ownership and CI-compliance gates.
    The sweep owns initial, dry-run-advance, and not_promoted candidate-advance
    triggers; dedicated bounded re-drivers retain every other trigger family.
    ``pre_sweep_status`` defers a state changed by an earlier leg until the next
    cycle. Returns True only when ``run_qualification`` reports ``spawned``.
    """
    from app import release_qualifier
    from app.qualification_triggers import (
        SWEEP_OWNED_TRIGGERS,
        resolve_qualification_trigger,
    )
    from app.runtime_config_state import get_auto_qualification_triggers_mode

    # ``_current_integration_audited_surface`` stays in the sweep module: it
    # is a SHARED read seam (``release_qualifier_redrive`` imports it back
    # from there too) and tests patch that single attribute for both
    # callers, so resolve it through the sweep instead of binding a private
    # copy here.
    from app.release_qualifier_sweep import (
        _current_integration_audited_surface,
    )

    knob = get_auto_qualification_triggers_mode()
    if knob == "off":
        return False

    factory = session_factory or AsyncSessionLocal
    async with factory() as fire_session:
        version = await fire_session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        if version.status != "open" or version.phase != "stabilize":
            return False
        qual = (
            version.last_qualification
            if isinstance(version.last_qualification, dict)
            else {}
        )
        status = qual.get("status")
        # Do not race an outcome written by an earlier leg in this cycle.
        if pre_sweep_status is not None and status != pre_sweep_status:
            logger.info(
                "[RELEASE_QUALIFIER] sweep: auto-trigger deferring version %s — "
                "qualification status moved %r -> %r during this sweep cycle",
                version_id,
                pre_sweep_status,
                status,
            )
            return False
        project = await fire_session.get(Project, version.project_id)
        if project is None:
            return False
        try:
            from app import category_verdict_ledger as _cvl
            from app.release_readiness import (
                auto_mark_stale_audit_findings,
                compute_requalification_readiness,
            )

            _is_candidate_fully_met = await _cvl.is_candidate_fully_met(
                fire_session, version.id, qual.get("candidate_sha")
            )
            current_surface = await _current_integration_audited_surface(
                fire_session, project
            )
            if current_surface:
                await auto_mark_stale_audit_findings(
                    fire_session,
                    version.project_id,
                    version.id,
                    current_surface,
                )
            _readiness = await compute_requalification_readiness(
                fire_session,
                version.project_id,
                candidate_surface_paths=current_surface,
            )
        except Exception:  # noqa: BLE001 — best-effort sweep; defer to next cycle
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: auto-trigger readiness resolution "
                "failed for version %s; deferring",
                version_id,
            )
            await fire_session.rollback()
            return False
        # Best-effort automain HEAD / CI state for the advanced-candidate legs
        # (T1 candidate-advanced, T4 post_dry_run_advance).  The gate never raises
        # and degrades to ``unavailable`` (automain_sha=None) without credentials,
        # which simply disables those legs — the same conservative behaviour as the
        # read surface, which passes None to avoid any GitHub I/O.
        automain_sha = None
        automain_ci_state = None
        try:
            from app.release_readiness import compute_promotion_ci_green_gate

            _gate = await compute_promotion_ci_green_gate(
                fire_session, version.project_id, dispatch=False
            )
            automain_sha = _gate.get("head_sha")
            _gstate = _gate.get("gate_state")
            if _gstate in ("red", "green", "green_rehearsal", "pending", "no_checks"):
                automain_ci_state = _gstate
        except Exception:  # noqa: BLE001 — best-effort; degrade to None
            logger.warning(
                "[RELEASE_QUALIFIER] sweep: automain HEAD resolution failed for "
                "version %s; auto-trigger running without the advanced-candidate "
                "legs",
                version_id,
                exc_info=True,
            )
        # Quiet window: a merge burst holds auto-fire until the tip settles.
        automain_head_quiet_seconds = await compute_automain_head_quiet_seconds(
            fire_session, version.project_id, automain_sha, version_id=version_id, now=_utcnow()
        )
        decision = resolve_qualification_trigger(
            phase=version.phase or "",
            status=status,
            error_code=qual.get("error") if status == "error" else None,
            qualification_attempts=version.qualification_attempts or 0,
            error_redrive_count=int(qual.get("error_redrive_count") or 0),
            is_candidate_fully_met=_is_candidate_fully_met,
            requalification_ready=_readiness.get("requalification_ready"),
            candidate_sha=qual.get("candidate_sha"),
            automain_sha=automain_sha,
            automain_ci_state=automain_ci_state,
            blocker=qual.get("blocker"),
            in_flight=status == "pending",
            promotion_check_wait=bool(qual.get("promotion_pending")),
            consecutive_infra_errors=int(qual.get("consecutive_infra_errors") or 0),
            rubric_redrive_count=int(qual.get("rubric_redrive_count") or 0),
            promotion_failure_count=int(qual.get("promotion_failure_count") or 0),
            automain_head_quiet_seconds=automain_head_quiet_seconds,
        )
        if not decision.fire:
            return False
        if knob == "observe":
            logger.info(
                "[RELEASE_QUALIFIER] sweep: auto-trigger OBSERVE would fire "
                "qualification for version %s (trigger=%s reason=%s)",
                version_id,
                decision.trigger,
                decision.reason,
            )
            return False

        # Raw status/blocker skips previously hid candidate_advanced for
        # non-coverage blockers. Apply executor ownership only after resolution.
        #
        # The claim "every OTHER trigger is owned by a dedicated re-driver" was
        # FALSE for ``blockers_cleared`` (promotion_failed) and
        # ``deferred_unsynced_redrive`` from commit a9ed8d6e until those two
        # re-drivers were restored: the resolver armed the version, this filter
        # discarded the decision, nothing re-fired qualification, and NOTHING
        # RECORDED THAT A DECISION HAD BEEN DISCARDED.  The routing tables now
        # live in ``qualification_triggers`` as data, and the discard below
        # stamps a breadcrumb naming the trigger and its owner -- alerting when
        # the named owner does not exist, because a trigger routed to a
        # non-existent owner is a permanent silent latch.
        if decision.trigger not in SWEEP_OWNED_TRIGGERS:
            await _record_discarded_trigger_routing(
                version_id, decision.trigger, session_factory=session_factory
            )
            return False

        # A CI-compliance deferral is durable and owned by the recheck loop above.
        # These execution guards deliberately run AFTER trigger resolution so
        # they cannot erase candidate invalidation from the decision surface.
        blocked = qual.get("qualify_request_blocked")
        if isinstance(blocked, dict) and blocked.get("blocker") == "ci_compliance":
            return False
        from app.strategic_spawning import ci_compliance_qualify_gate

        if ci_compliance_qualify_gate(project).get("blocked"):
            logger.info(
                "[RELEASE_QUALIFIER] sweep: deferring auto-trigger for v%d — "
                "stored CI-compliance record shows the integration HEAD breaches "
                "the guardrails",
                version.version_number,
            )
            return False
        # Defer while a prior attempt's opus-tier audit pair is still running
        # (see release_qualifier_debounce) — the lane's finalize updates
        # ``last_qualification`` and a later sweep re-resolves cleanly.
        _live_audit_id = await find_live_qualifier_audit_task_id(fire_session, version.project_id)
        if _live_audit_id is not None:
            logger.info(
                "[RELEASE_QUALIFIER] sweep: deferring auto-trigger for v%d — "
                "qualifier audit task %s is already live",
                version.version_number,
                _live_audit_id,
            )
            return False
        result = await release_qualifier.run_qualification(
            fire_session,
            version.project_id,
            version,
            auto_trigger=True,
        )
        await fire_session.commit()
        if result and result.get("status") == "spawned":
            logger.info(
                "[RELEASE_QUALIFIER] sweep auto-fired qualification for version "
                "%s (trigger=%s reason=%s)",
                version_id,
                decision.trigger,
                decision.reason,
            )
            return True
        logger.info(
            "[RELEASE_QUALIFIER] sweep auto-trigger fired run_qualification for "
            "version %s but it did not spawn (status=%s)",
            version_id,
            (result or {}).get("status"),
        )
        return False
