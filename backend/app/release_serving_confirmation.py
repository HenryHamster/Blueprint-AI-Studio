"""Post-promotion serving confirmation and deploy-failure recovery.

Extracted from ``app.release_reconciliation`` so that module stays under its
size cap.  This module owns the post-promotion serving confirmation cluster:
refreshing release artifact rows, deriving the serving outcome, confirming a
promoted version is actually serving, recording the activation receipt, and
recovering from ``deploy_failed`` / rollout states.

``app.release_reconciliation`` re-exports the helpers defined here so existing
importers (``app.release_qualifier_sweep``, ``app.routers.admin_release_version``,
and the test suites) keep resolving them through the canonical module.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app import release_version
from app.database import AsyncSessionLocal
from app.models import (
    DeploymentState,
    DeploymentStatus,
    Project,
    QualificationVerdict,
    ReleaseDeployArtifact,
)
from app.deploy_identity import deploy_repo_identity
from app.release_artifacts import (
    BACKEND_REVISION_ARTIFACT_KIND,
    aggregate_required_artifact_status,
    apply_serving_decision,
    artifact_is_unconfirmed,
    get_or_create_release_deploy_artifact,
    is_backend_revision_artifact,
    iter_release_artifact_providers,
    observable_release_artifact_providers,
    project_backend_revision_artifact,
    summarize_release_artifacts,
)
from app.release_promotion import _DEPLOY_CONFIRM_WINDOW_SECONDS
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _refresh_release_artifact_rows(
    session,
    version: release_version.ReleaseVersion,
    now: datetime,
    *,
    recheck_failed_required: bool = False,
) -> tuple[
    list[tuple[object, object]],
    bool,
    bool,
    object | None,
]:
    """Refresh artifact rows from provider checks without mutating version scalars.

    ``_confirm_serving_or_fail`` projects the backend artifact onto the legacy
    ``ReleaseVersion`` scalars while the multi-artifact registry is still
    unresolved, but recovery/admin clear paths cannot do that: they start from a
    latched aggregate ``deploy_failed`` scalar and must not transiently rewrite
    it back to backend-only truth before every required artifact re-confirms.
    This helper centralizes the provider sweep and returns the backend artifact
    for the caller to project or consume explicitly.
    """

    provider_rows = []
    had_required_unconfirmed = False
    mutated = False
    backend_artifact = None

    # Iterate only the providers OBSERVABLE for this version's project (the
    # deploy-level identity), not every registered provider.  This is the same
    # set armed at promotion: a non-observable provider (e.g. a Cloud Run signal
    # for an external project) must never get a row created or checked here, or
    # its unconfirmed row would strand the aggregate on a signal nothing can
    # read.  Resolving the deploy identity once per call keeps the sweep cheap.
    project = await session.get(Project, version.project_id)
    deploy_identity = deploy_repo_identity()

    observable_kinds: set[str] = set()
    for provider in observable_release_artifact_providers(project, deploy_identity):
        artifact, created = await get_or_create_release_deploy_artifact(
            session,
            version,
            provider.artifact_kind,
            confirmation_deadline=version.deploy_confirm_deadline,
        )
        if created:
            mutated = True
        if provider.required and artifact_is_unconfirmed(artifact):
            had_required_unconfirmed = True

        should_check = artifact_is_unconfirmed(artifact) or (
            recheck_failed_required
            and provider.required
            and artifact.serving_status in ("deploy_failed", "confirmation_timed_out")
        )
        if should_check:
            outcome = await provider.check(session, version, artifact, now)
            if outcome is not None:
                if apply_serving_decision(artifact, outcome, now=now):
                    mutated = True
                session.add(artifact)
        provider_rows.append((provider, artifact))
        observable_kinds.add(provider.artifact_kind)
        if is_backend_revision_artifact(provider.artifact_kind):
            backend_artifact = artifact

    # Also resolve any existing REQUIRED artifact rows whose provider is NOT
    # currently observable for this project.  Such rows were armed when the
    # deploy identity matched (or by a prior code version) but can never
    # self-confirm now.  Time them out at their confirmation_deadline with
    # reason_code="provider_unobservable" so the sweep aggregate and
    # summarize_release_artifacts converge and no armed row can outlive
    # resolution.  Within the deadline, count them as unconfirmed so the sweep
    # re-evaluates on each tick (the identity may be restored before deadline).
    all_required_provider_map = {
        p.artifact_kind: p for p in iter_release_artifact_providers() if p.required
    }
    unobservable_required_kinds = set(all_required_provider_map) - observable_kinds
    if unobservable_required_kinds:
        existing_unobservable_rows = (
            (
                await session.execute(
                    select(ReleaseDeployArtifact).where(
                        ReleaseDeployArtifact.version_id == version.id,
                        ReleaseDeployArtifact.artifact_kind.in_(
                            unobservable_required_kinds
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        _now_naive = _to_naive_utc(now) or now
        for artifact in existing_unobservable_rows:
            provider = all_required_provider_map[artifact.artifact_kind]
            if not artifact_is_unconfirmed(artifact):
                # Already resolved — include in provider_rows for aggregate.
                provider_rows.append((provider, artifact))
                continue
            # Unconfirmed unobservable required row: record it as having been
            # unconfirmed so _confirm_serving_or_fail proceeds to compute the
            # aggregate (rather than bailing at the had_required_unconfirmed
            # guard), then resolve or leave it depending on the deadline.
            had_required_unconfirmed = True
            artifact_deadline = _to_naive_utc(
                artifact.confirmation_deadline or version.deploy_confirm_deadline
            )
            if artifact_deadline is not None and _now_naive >= artifact_deadline:
                # Past deadline: resolve with provider_unobservable so the row
                # is no longer pending and the aggregate can converge.
                decision: tuple = (
                    "confirmation_timed_out",
                    "provider_unobservable",
                    None,
                )
                if apply_serving_decision(artifact, decision, now=now):
                    mutated = True
                session.add(artifact)
            # Within deadline: count as unconfirmed (identity may be restored
            # before the window expires); had_required_unconfirmed already set.
            provider_rows.append((provider, artifact))

    return provider_rows, had_required_unconfirmed, mutated, backend_artifact


# ---------------------------------------------------------------------------
# Post-promotion serving confirmation
# ---------------------------------------------------------------------------


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """Strip tzinfo so comparisons against the naive-UTC DeploymentState match."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _classify_serving_outcome(
    ds: DeploymentState | None,
    promoted_at: datetime | None,
    promoted_sha: str | None,
    deadline: datetime | None,
    now: datetime,
) -> tuple[str, str | None, datetime | None] | None:
    """Derive a post-promotion serving outcome from the deploy-health signal.

    Pure function (no I/O) so the decision is directly unit-testable.  Returns:

      * ``None`` — no decision yet: still inside the confirmation window and no
        new healthy revision has booted (keep waiting).
      * ``("serving_confirmed", served_sha, served_at)`` — a new revision booted
        AFTER ``promoted_at``, is STABLE, and its live serving SHA was
        INDEPENDENTLY observed to match ``promoted_sha``.  ``served_sha`` is
        always that observed value, never a ``promoted_sha`` stand-in.
      * ``("deploy_failed", reason)`` (third element ``None``) — the deploy
        ACTIVELY went bad: a ROLLED_BACK/HALTED state arose after the promotion
        (``reason`` ``rolled_back``/``halted``), or the observed serving SHA does
        not match the promoted SHA (``sha_mismatch``).  A STABLE ``sha_mismatch``
        is AMBIGUOUS rather than an outright failure: the consumer layer
        (``_reclassify_sha_mismatch_if_superseded``) re-checks it against GitHub
        ancestry and resolves a descendant serving revision to
        ``serving_confirmed`` (supersession) while keeping non-descendant /
        indeterminate outcomes conservative.  Emitting it here for ANY STABLE
        mismatch — regardless of the deployed_at/promoted_at ordering — is what
        lets the merge-commit promotion (deploy.yml deploys the merge commit;
        ``promoted_sha`` records the automain tip) confirm even when the deploy
        was observed before the orchestrator stamped ``promoted_at``.
      * ``("confirmation_timed_out", reason)`` (third element ``None``) — the
        bounded confirmation window elapsed WITHOUT a positive confirmation but
        also WITHOUT any active failure signal: no new healthy revision appeared
        by ``deadline`` (``no_new_revision``), or a new revision went STABLE but
        its serving SHA could never be independently observed
        (``serving_sha_unobservable`` — e.g. the deploy SHA could not be read).
        This is NOT a failed deploy — the code likely shipped but is unverified;
        it must be surfaced and treated DIFFERENTLY from ``deploy_failed`` (a
        softer "verify serving" signal, not a roll-forward crisis), and it is
        recoverable to ``serving_confirmed`` if the SHA is later observed serving.

    Backstop: a promoted version with no explicit ``deadline`` would otherwise be
    undecidable forever (the timeout branch can never fire).  When ``deadline``
    is missing, fall back to ``promoted_at + _DEPLOY_CONFIRM_WINDOW_SECONDS`` so
    confirmation is always bounded — no version can wedge in ``unconfirmed``.
    """
    promoted_at = _to_naive_utc(promoted_at)
    deadline = _to_naive_utc(deadline)
    now = _to_naive_utc(now) or now

    # Backstop the confirmation window: a promotion recorded without a stamped
    # deadline (legacy / a promotion path that skipped arming it) must still time
    # out, or it sits unconfirmed forever and gates the next version.
    if deadline is None and promoted_at is not None:
        deadline = promoted_at + timedelta(seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS)

    # A new revision went STABLE after the promotion, but its live serving SHA
    # could not be independently observed (Cloud Run lookup degraded to no
    # value).  Tracked so the deadline path lands a distinct, honest failure
    # reason instead of confirming with a fabricated served SHA.
    serving_sha_unobservable = False

    if ds is not None:
        deployed_at = _to_naive_utc(ds.deployed_at)
        halted_at = _to_naive_utc(getattr(ds, "halted_at", None))
        status = ds.status
        revision_after_promotion = (
            deployed_at is not None
            and promoted_at is not None
            and deployed_at > promoted_at
        )

        # A HALT or ROLLBACK that occurred at/after the promotion is a hard
        # failure regardless of the deadline — the deploy actively went bad.
        if status == DeploymentStatus.HALTED:
            ref = halted_at or deployed_at
            if ref is not None and promoted_at is not None and ref >= promoted_at:
                return ("deploy_failed", "halted", None)
        if (
            status == DeploymentStatus.ROLLED_BACK
            and deployed_at is not None
            and promoted_at is not None
            and deployed_at >= promoted_at
        ):
            return ("deploy_failed", "rolled_back", None)

        # SERVING CONFIRMED: the deployment is STABLE and the live serving SHA
        # independently matches the promoted SHA.  A pre-promotion revision
        # CANNOT serve the promoted merge SHA (that SHA did not exist before
        # the promotion commit), so an exact SHA match is positive evidence
        # regardless of deployed_at/promoted_at ordering.  The race case where
        # the Cloud Run revision booted before the orchestrator finished
        # recording promoted_at (deployed_at <= promoted_at) is safe to
        # confirm: the matched SHA proves this is the right revision.
        if status == DeploymentStatus.STABLE:
            current_sha = ds.current_sha or None
            if current_sha and promoted_sha:
                if current_sha == promoted_sha:
                    return ("serving_confirmed", current_sha, deployed_at)
                # A STABLE revision serving a DIFFERENT SHA is AMBIGUOUS: it may
                # be the deploy landing as the promotion MERGE COMMIT (a
                # descendant of promoted_sha — the v25/v27 pattern where
                # deploy.yml deploys github.sha = merge commit while promoted_sha
                # records the automain tip), a wrong/rolled-back revision, or the
                # OLD revision still serving while the deploy is in flight.  The
                # consumer-layer descendant check
                # (_reclassify_sha_mismatch_if_superseded) resolves the ambiguity
                # via GitHub ancestry.  Do NOT gate it on revision_after_promotion:
                # a merge commit's deploy can be observed before the orchestrator
                # records promoted_at (deployed_at <= promoted_at), and that
                # descendant MUST confirm.  Emitting sha_mismatch here routes every
                # STABLE mismatch through that check; non-descendant outcomes keep
                # their conservative wait / honest-fail semantics there.
                return ("deploy_failed", "sha_mismatch", None)
            elif revision_after_promotion:
                # New revision booted after promotion but serving SHA is
                # unobservable (Cloud Run lookup degraded) — not confirmable.
                serving_sha_unobservable = True

    # No confirmation yet — once the bounded window elapses this is a TIMEOUT,
    # not an active failure: nothing observed the deploy going bad, the promoted
    # SHA just could not be confirmed serving in time.  Surface it distinctly
    # from deploy_failed (rolled_back/halted/sha_mismatch above) so it reads as
    # "verify serving", not "recover a failed deploy".
    if deadline is not None and now >= deadline:
        if serving_sha_unobservable:
            return ("confirmation_timed_out", "serving_sha_unobservable", None)
        return ("confirmation_timed_out", "no_new_revision", None)
    return None


async def _record_post_promotion_outcome(
    session,
    version: release_version.ReleaseVersion,
    outcome: str,
    now: datetime,
) -> None:
    """Best-effort: stamp ``post_promotion_outcome`` on the version's verdict row.

    Records the resolved serving outcome on the immutable
    ``QualificationVerdict`` history (the row written when the version promoted)
    without mutating any prior immutable field.  Mirrors
    ``_record_qualification_verdict``'s SAVEPOINT discipline: the caller's
    already-staged ReleaseVersion mutations are flushed into the outer
    transaction first, then the verdict update runs inside ``begin_nested`` so a
    failure (e.g. no verdict row) rolls back only the nested savepoint and never
    the serving-status write.
    """
    try:
        verdict = (
            (
                await session.execute(
                    select(QualificationVerdict)
                    .where(
                        QualificationVerdict.release_version_id == version.id,
                        QualificationVerdict.terminal_status == "promoted",
                    )
                    .order_by(QualificationVerdict.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if verdict is None:
            return
        await session.flush()
        async with session.begin_nested():
            verdict.post_promotion_outcome = outcome
            verdict.post_promotion_outcome_at = now
            session.add(verdict)
            await session.flush()
    except Exception as exc:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.warning(
            "[RELEASE_QUALIFIER] failed to record post-promotion outcome for v%s "
            "(non-fatal — additive history only): %s",
            getattr(version, "version_number", "?"),
            exc,
        )


async def _confirm_serving_or_fail(
    version_id: uuid.UUID,
    *,
    session_factory=None,
    now: datetime | None = None,
) -> bool:
    """Resolve a provisional (``serving_status="unconfirmed"``) promotion.

    For a ``promoted`` version still awaiting serving confirmation, derive the
    outcome from the DeploymentState singleton (the existing deploy-health
    serving signal) and either confirm it serving or land a distinct loud
    deploy-failed state.  Runs in its own dedicated session under a row lock —
    the same per-version isolation pattern as the reconcile loops above — so a
    failure on one version cannot touch another's committed work.

    Returns True iff the version transitioned out of ``unconfirmed`` this call.
    """
    factory = session_factory or AsyncSessionLocal
    _now = now or _utcnow()
    async with factory() as session:
        try:
            version: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            # Re-check under the lock: a concurrent sweep may have resolved it.
            if version.status != "promoted":
                return False
            # Backstop: a promotion recorded without a confirmation deadline (a
            # legacy promotion path that skipped arming it) would never time out
            # and would wedge the next version forever.  Arm it durably from
            # promoted_at so confirmation is always bounded and the artifact rows
            # created below inherit a real deadline.
            if (
                version.deploy_confirm_deadline is None
                and version.promoted_at is not None
            ):
                version.deploy_confirm_deadline = version.promoted_at + timedelta(
                    seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS
                )
                session.add(version)
            (
                provider_rows,
                had_required_unconfirmed,
                mutated,
                backend_artifact,
            ) = await _refresh_release_artifact_rows(session, version, _now)

            if not provider_rows:
                return False

            if not had_required_unconfirmed:
                if mutated:
                    await session.commit()
                return False

            if backend_artifact is not None:
                if project_backend_revision_artifact(version, backend_artifact):
                    mutated = True
                session.add(version)

            aggregate_status, aggregate_reason = aggregate_required_artifact_status(
                provider_rows
            )
            # Undecidable corruption: a promotion with NO recorded promoted_sha
            # can never confirm by SHA-match. Once the (backstopped)
            # confirmation window has elapsed, resolve it as the explicit
            # promoted_sha_missing TIMEOUT, regardless of whether one provider
            # already emitted its generic serving-unobservable timeout. This is
            # not a deploy_failed: nothing went wrong, we simply cannot verify
            # what shipped.
            if (
                aggregate_status in ("unconfirmed", "confirmation_timed_out")
                and not version.promoted_sha
            ):
                _deadline = _to_naive_utc(version.deploy_confirm_deadline)
                _now_naive = _to_naive_utc(_now) or _now
                if _deadline is not None and _now_naive >= _deadline:
                    aggregate_status, aggregate_reason = (
                        "confirmation_timed_out",
                        "promoted_sha_missing",
                    )
            if aggregate_status == "unconfirmed":
                if mutated:
                    await session.commit()
                return False

            if aggregate_status == "serving_confirmed":
                version.serving_status = "serving_confirmed"
                version.deploy_failure_reason = None
                session.add(version)
                from app.release_commit_authorship import (
                    persist_serving_authorship_evidence,
                )

                authorship_plan = await persist_serving_authorship_evidence(
                    session, version
                )
                await _record_post_promotion_outcome(
                    session, version, "serving_confirmed", _now
                )
                from app.notification_dispatch import post_event_message

                await post_event_message(
                    session,
                    version.project_id,
                    event_type="release.serving_confirmed",
                    summary=(
                        f"Promoted v{version.version_number} is serving "
                        f"{(version.served_sha or version.promoted_sha or '')[:12]}."
                    ),
                    context={
                        "release_version_id": str(version.id),
                        "version_number": version.version_number,
                        "serving_status": "serving_confirmed",
                        "promoted_sha": version.promoted_sha,
                        "served_sha": version.served_sha,
                        "served_at": (
                            version.served_at.isoformat() if version.served_at else None
                        ),
                        "qualification_status": (version.last_qualification or {}).get(
                            "status"
                        ),
                        "receipt_kind": "serving_barrier",
                        "resident_wake_barrier": True,
                    },
                    idempotency_key=f"release:{version.id}:serving:confirmed",
                )
                await session.commit()
                if authorship_plan is not None:
                    from app.release_commit_authorship import (
                        measure_serving_authorship_evidence,
                    )

                    await measure_serving_authorship_evidence(
                        version.id,
                        expected_receipt_identity=authorship_plan["receipt_identity"],
                    )
                logger.info(
                    "[RELEASE_QUALIFIER] serving confirmed for promoted v%d "
                    "(served_sha=%s)",
                    version.version_number,
                    (version.served_sha or "")[:12],
                )
                return True

            if aggregate_status == "confirmation_timed_out":
                # The window elapsed without confirmation, but nothing was
                # observed going bad (no rollback/halt/sha-mismatch).  This is
                # NOT a failed deploy: surface a distinct, softer state so the
                # operator/strategist verify serving rather than treating it as
                # a crisis.  No loud deploy-failed ERROR alert; recoverable to
                # serving_confirmed if the SHA is later observed serving.
                version.serving_status = "confirmation_timed_out"
                version.deploy_failure_reason = aggregate_reason
                session.add(version)
                await _record_post_promotion_outcome(
                    session, version, "confirmation_timed_out", _now
                )
                from app.notification_dispatch import post_event_message

                await post_event_message(
                    session,
                    version.project_id,
                    event_type="release.serving_confirmation_timed_out",
                    summary=(
                        f"Serving confirmation timed out for promoted "
                        f"v{version.version_number}: {aggregate_reason}."
                    ),
                    context={
                        "release_version_id": str(version.id),
                        "version_number": version.version_number,
                        "serving_status": "confirmation_timed_out",
                        "promoted_sha": version.promoted_sha,
                        "qualification_status": (version.last_qualification or {}).get(
                            "status"
                        ),
                        "reason": aggregate_reason,
                        "receipt_kind": "serving_barrier",
                        "resident_wake_barrier": True,
                    },
                    idempotency_key=f"release:{version.id}:serving:timed_out",
                )
                try:
                    from app.strategic_trigger import request_strategic_refresh

                    await request_strategic_refresh(
                        session,
                        version.project_id,
                        f"promotion_confirmation_timed_out:{aggregate_reason}",
                    )
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] strategic refresh arm raised for "
                        "timed-out v%d (non-fatal)",
                        version.version_number,
                    )
                await session.commit()
                logger.warning(
                    "[RELEASE_QUALIFIER] serving confirmation TIMED OUT for "
                    "promoted v%d (reason=%s) — promoted with no active deploy "
                    "failure, but the promoted SHA could not be confirmed serving "
                    "within the window",
                    version.version_number,
                    aggregate_reason,
                )
                return True

            version.serving_status = "deploy_failed"
            version.deploy_failure_reason = aggregate_reason
            session.add(version)
            _aggregate_summary, _artifact_evidence = summarize_release_artifacts(
                version,
                [artifact for _provider, artifact in provider_rows],
                project=await session.get(Project, version.project_id),
            )
            await _record_post_promotion_outcome(
                session, version, "deploy_failed", _now
            )
            from app.notification_dispatch import post_event_message

            await post_event_message(
                session,
                version.project_id,
                event_type="release.serving_failed",
                summary=(
                    f"Serving failed for promoted v{version.version_number}: "
                    f"{aggregate_reason}."
                ),
                context={
                    "release_version_id": str(version.id),
                    "version_number": version.version_number,
                    "serving_status": "deploy_failed",
                    "promoted_sha": version.promoted_sha,
                    "qualification_status": (version.last_qualification or {}).get(
                        "status"
                    ),
                    "reason": aggregate_reason,
                    "receipt_kind": "serving_barrier",
                    "resident_wake_barrier": True,
                },
                idempotency_key=f"release:{version.id}:serving:failed",
            )
            # Loud operator alert (ERROR), deduped via deploy_confirm_alerted_at.
            try:
                from app.release_stall_alerts import _alert_promotion_deploy_failed

                await _alert_promotion_deploy_failed(
                    session,
                    version,
                    artifact_evidence=_artifact_evidence,
                    aggregate_deploy_failure_reason=_aggregate_summary["reason_code"],
                )
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] deploy-failed alert raised for v%d "
                    "(non-fatal)",
                    version.version_number,
                )
            # Wake the strategist now instead of leaving it to discover the
            # failed deploy on its next natural cycle (gated behind the
            # strategic-spawn cooldown). A failed production deploy is the
            # loudest release signal, so arm the same coalescing refresh marker
            # the post-qualification path uses. Best-effort and non-fatal:
            # request_strategic_refresh swallows its own errors, does NOT commit
            # (rides this transaction), and the single-slot marker coalesces, so
            # arming can never abort the deploy-failed write and a re-scanned
            # version cannot re-fire (the serving_status!=unconfirmed guard above
            # means this branch runs exactly once per version).
            try:
                from app.strategic_trigger import request_strategic_refresh

                await request_strategic_refresh(
                    session,
                    version.project_id,
                    f"promotion_deploy_failed:{aggregate_reason}",
                )
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] strategic refresh arm raised for v%d "
                    "(non-fatal)",
                    version.version_number,
                )
            await session.commit()
            logger.error(
                "[RELEASE_QUALIFIER] promotion deploy FAILED for v%d "
                "(reason=%s) — promoted SHA never confirmed serving",
                version.version_number,
                aggregate_reason,
            )
            return True
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] serving-confirmation: unexpected error for "
                "version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] serving-confirmation: rollback failed "
                    "for version %s",
                    version_id,
                )
            return False


# ---------------------------------------------------------------------------
# Post-promotion deploy-failure recovery
# ---------------------------------------------------------------------------
#
# A ``serving_status="deploy_failed"`` latch is NOT terminal-by-construction.  A
# deploy that boot-crashed and then recovered, or that was merely slow to come up
# past the bounded confirmation window (the real v5 pattern: promoted 14:58 UTC,
# only began serving at 19:51 UTC), would otherwise be rendered permanently
# untrue across every surface — version status, the operator deploy-failed ERROR
# alert, the strategist "recover the failed deploy" crisis directive, and the
# QualificationVerdict.post_promotion_outcome precision sample.  When such a
# version is later observed to ACTUALLY serve its promoted SHA, the recovery path
# reconciles it back to a truthful serving_confirmed state and clears every
# derived failure surface — bounded by a recovery window so a genuinely dead
# deploy still stays loudly failed forever.
_DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS_DEFAULT = 24 * 3600


async def _recover_serving_confirmed(
    session,
    version: release_version.ReleaseVersion,
    served_sha: str,
    served_at: datetime | None,
    now: datetime,
    *,
    confirmed_reason_code: str | None = None,
) -> dict[str, Any] | None:
    """Flip a deploy_failed version back to serving_confirmed and clear surfaces.

    Shared mutation helper for the deploy-failure recovery path.  Given a version
    whose promoted SHA is now INDEPENDENTLY observed serving, it:

      * flips ``serving_status`` ``deploy_failed`` -> ``serving_confirmed``,
      * stamps ``served_sha`` / ``served_at`` from the confirmed observation,
      * clears ``deploy_failure_reason``,
      * updates the ``QualificationVerdict.post_promotion_outcome`` to
        ``serving_confirmed`` (reusing ``_record_post_promotion_outcome``'s
        SAVEPOINT discipline so the immutable verdict history is appended-to, not
        mutated, and a verdict failure never rolls back the serving-status write),
      * resolves the open promotion-deploy-failed operator alert, and
      * arms a coalescing strategic refresh so the crisis directive recomputes
        against the now-true serving_confirmed state this cycle.

    ``confirmed_reason_code`` optionally annotates the confirmed backend artifact
    (e.g. ``superseded_by_descendant`` when the recovery re-check resolved a
    latched sha_mismatch as supersession); left ``None`` the confirmation clears
    the artifact reason code, exactly as before.

    Runs on the caller's session.  Authorship evidence deliberately commits its
    pending marker before GitHub I/O and performs its final write under a short
    CAS lock; the remaining recovery projection is committed by the caller.
    """
    backend_artifact, _created = await get_or_create_release_deploy_artifact(
        session,
        version,
        BACKEND_REVISION_ARTIFACT_KIND,
        confirmation_deadline=version.deploy_confirm_deadline,
    )
    apply_serving_decision(
        backend_artifact,
        ("serving_confirmed", served_sha, served_at, confirmed_reason_code),
        now=now,
    )
    session.add(backend_artifact)
    project_backend_revision_artifact(version, backend_artifact)
    version.deploy_failure_reason = None
    session.add(version)
    from app.release_commit_authorship import (
        persist_serving_authorship_evidence,
    )

    authorship_plan = await persist_serving_authorship_evidence(session, version)
    await _record_post_promotion_outcome(session, version, "serving_confirmed", now)
    # Resolve the loud deploy-failed operator alert — it no longer describes a
    # real condition once the promoted SHA is confirmed serving.  Best-effort and
    # non-fatal: a resolve failure must never abort the recovery write.
    try:
        from app.release_stall_alerts import _resolve_promotion_deploy_failed_alert

        await _resolve_promotion_deploy_failed_alert(session, version)
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] deploy-failed alert resolve raised for "
            "recovered v%d (non-fatal)",
            version.version_number,
        )
    # Wake the strategist so the "recover the failed deploy" crisis directive
    # recomputes against the now-true serving_confirmed state instead of waiting
    # for the next natural cycle.  Best-effort and non-fatal:
    # request_strategic_refresh swallows its own errors, does NOT commit (rides
    # this transaction), and the single-slot marker coalesces.
    try:
        from app.strategic_trigger import request_strategic_refresh

        await request_strategic_refresh(
            session,
            version.project_id,
            "promotion_deploy_recovered",
        )
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] strategic refresh arm raised for recovered "
            "v%d (non-fatal)",
            version.version_number,
        )

    # The caller commits the serving truth and all recovery projections.  Only
    # after that durable boundary may the external authorship measurement run.
    # Recovery callers that need the plan can consume this private return value.
    return authorship_plan


async def _record_activation_receipt(
    session,
    version: release_version.ReleaseVersion,
    *,
    revision: str | None,
    host: str | None,
    operator_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Record an operator activation of a promoted version and close the serving loop.

    ``blueprint deploy`` now binds activation to the backend's latest promoted
    version and POSTs a receipt after the control-plane SSH activation succeeds.
    This helper makes that receipt durable and authoritative:

      * stamps activation provenance on ``version.last_qualification``
        (reassigning the dict so SQLAlchemy detects the JSON change, mirroring
        ``_record_deploy_failed_clear_attestation``);
      * confirms the ``backend_revision`` artifact with the served identifier —
        the reported ``revision``, falling back to the version's ``promoted_sha`` —
        and projects it onto the legacy scalar;
      * refreshes the full required-artifact registry (rechecking failed rows);
      * when the required-artifact aggregate resolves to ``serving_confirmed``,
        delegates to ``_recover_serving_confirmed`` so the whole version flips
        (served_sha/served_at, verdict outcome, alert resolve, strategic refresh);
      * when a required non-backend artifact is still pending, leaves the whole
        version unconfirmed — the receipt-confirmed backend artifact stands and
        the ``_confirm_serving_or_fail`` sweep finishes the remaining gate.

    Runs on the caller's already-locked session; does NOT commit (the caller
    commits under the same per-version row lock).
    """
    served_sha = (revision or version.promoted_sha or "").strip()
    backend_artifact, _created = await get_or_create_release_deploy_artifact(
        session,
        version,
        BACKEND_REVISION_ARTIFACT_KIND,
        confirmation_deadline=version.deploy_confirm_deadline,
    )
    if served_sha:
        apply_serving_decision(
            backend_artifact,
            ("serving_confirmed", served_sha, now, None),
            now=now,
        )
        session.add(backend_artifact)

    qual = dict(version.last_qualification or {})
    qual["activation_receipt"] = {
        "mode": "approved_activation" if version.activation_requested_at else "cli_activation",
        "operator": operator_id,
        "revision": served_sha or None,
        "host": host,
        "activated_at": now.isoformat(),
        # Provenance for an approved execution: the approval this receipt fulfilled.
        "activation_requested_at": version.activation_requested_at.isoformat() if version.activation_requested_at else None,
        "activation_requested_by": version.activation_requested_by,
    }
    version.last_qualification = qual
    session.add(version)

    # Make the receipt-confirmed backend artifact durable in this transaction
    # BEFORE the provider sweep, so ``_refresh_release_artifact_rows``'s
    # get_or_create finds the confirmed row instead of manufacturing a second,
    # unconfirmed ``backend_revision`` row (invisible to its SELECT under a
    # no-autoflush session) and racing the UNIQUE(version_id, artifact_kind)
    # constraint on commit.
    await session.flush()

    provider_rows, _had, _mutated, _backend = await _refresh_release_artifact_rows(
        session,
        version,
        now,
        recheck_failed_required=True,
    )
    aggregate_status, aggregate_reason = aggregate_required_artifact_status(
        provider_rows
    )
    if aggregate_status == "serving_confirmed":
        await _recover_serving_confirmed(
            session,
            version,
            served_sha,
            now,
            now,
            confirmed_reason_code=backend_artifact.reason_code,
        )
        return {
            "mode": "approved_activation" if version.activation_requested_at else "cli_activation",
            "serving_status": "serving_confirmed",
            "served_sha": served_sha,
        }
    if backend_artifact is not None and project_backend_revision_artifact(
        version, backend_artifact
    ):
        session.add(version)
    return {
        "mode": "approved_activation" if version.activation_requested_at else "cli_activation",
        "serving_status": aggregate_status,
        "served_sha": served_sha,
        "reason_code": aggregate_reason,
    }


async def _reconcile_if_deploy_failed_recovered(
    version_id: uuid.UUID,
    *,
    session_factory=None,
    now: datetime | None = None,
    recovery_window_seconds: float = _DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS_DEFAULT,
) -> bool:
    """Recover a ``deploy_failed`` version whose promoted SHA is now serving.

    For a ``promoted`` version latched to ``serving_status="deploy_failed"`` (or
    ``confirmation_timed_out``), re-run the full required-artifact truth recheck
    against the live DeploymentState.  When the promoted SHA is now INDEPENDENTLY
    observed serving — the backend_revision provider confirming an observed SHA
    that equals OR is an ancestry-verified descendant of ``promoted_sha`` AND the
    full required-artifact aggregate rechecks as ``serving_confirmed`` — flip it
    back to a truthful ``serving_confirmed`` state via ``_recover_serving_confirmed``
    and clear every derived failure surface.  Any other verdict (still failed,
    rolled-back, halted, determinate non-ancestry sha_mismatch, a still-failed
    required non-backend artifact, or an indeterminate check) leaves the version
    deploy_failed — only the artifact-row truth actually learned is committed.

    Recovery is gated on the deterministic positive, NOT on elapsed time: an
    ancestry-verified deploy that the descendant-aware provider now confirms
    serving recovers even when the old recovery window has long since elapsed,
    because the truth recheck — not a timer — decides.  The
    ``recovery_window_seconds`` parameter is retained for signature/back-compat
    with the sweep call site and tests but no longer short-circuits the recheck;
    every negative or indeterminate result stays latched ``deploy_failed`` by the
    aggregate gate, so an elapsed window can never force-clear a dead deploy.

    Runs in its own dedicated session under a row lock — the same per-version
    isolation pattern as ``_confirm_serving_or_fail`` — so a failure on one
    version cannot touch another's committed work.  Returns True iff the version
    recovered this call.
    """
    factory = session_factory or AsyncSessionLocal
    _now = now or _utcnow()
    async with factory() as session:
        try:
            version: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            # Re-check under the lock: a concurrent sweep may have resolved it.
            if version.status != "promoted":
                return False
            # Both an active failure (deploy_failed) and a confirmation timeout
            # (confirmation_timed_out) are non-terminal: if the promoted SHA is
            # later observed serving, recover either to serving_confirmed.
            if version.serving_status not in (
                "deploy_failed",
                "confirmation_timed_out",
            ):
                return False

            # Recovery is gated on the deterministic positive below (a full
            # required-artifact serving_confirmed aggregate, including the
            # descendant-aware backend_revision provider), NOT on elapsed time.
            # The ``recovery_window_seconds`` param is retained for back-compat
            # but intentionally no longer short-circuits this recheck: an
            # ancestry-verified deploy must be able to recover even beyond the
            # old window, while every negative/indeterminate result stays latched
            # deploy_failed via the aggregate gate.
            _ = recovery_window_seconds

            (
                provider_rows,
                _,
                mutated,
                backend_artifact,
            ) = await _refresh_release_artifact_rows(
                session,
                version,
                _now,
                recheck_failed_required=True,
            )
            aggregate_status, _ = aggregate_required_artifact_status(provider_rows)
            backend_is_serving = (
                backend_artifact is not None
                and backend_artifact.serving_status == "serving_confirmed"
                and backend_artifact.served_identifier is not None
            )
            # Recovery now means the FULL required-artifact registry has
            # reconfirmed, not merely the backend-revision artifact. Commit any
            # artifact-row truth we learned even when the aggregate version state
            # must remain deploy_failed.
            if aggregate_status != "serving_confirmed" or not backend_is_serving:
                if mutated:
                    await session.commit()
                return False
            authorship_plan = await _recover_serving_confirmed(
                session,
                version,
                backend_artifact.served_identifier,
                backend_artifact.served_at,
                _now,
                # Preserve a supersession annotation set by the provider
                # re-check (a latched sha_mismatch resolved as
                # superseded_by_descendant); None for ordinary recoveries.
                confirmed_reason_code=backend_artifact.reason_code,
            )
            await session.commit()
            if authorship_plan is not None:
                from app.release_commit_authorship import (
                    measure_serving_authorship_evidence,
                )

                await measure_serving_authorship_evidence(
                    version.id,
                    expected_receipt_identity=authorship_plan["receipt_identity"],
                )
            logger.info(
                "[RELEASE_QUALIFIER] deploy-failed RECOVERED for promoted v%d "
                "(served_sha=%s) — promoted SHA confirmed serving after a prior "
                "deploy_failed latch",
                version.version_number,
                (version.served_sha or "")[:12],
            )
            return True
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] deploy-failed recovery: unexpected error "
                "for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] deploy-failed recovery: rollback failed "
                    "for version %s",
                    version_id,
                )
            return False
