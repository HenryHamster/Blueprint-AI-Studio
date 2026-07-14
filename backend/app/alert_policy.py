"""Project-scoped alert domain policy.

The alert table is shared infrastructure.  Domain-specific producers should
check this module before creating durable rows so Blueprint-only workflows do
not leak into every project's alert queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.deploy_identity import project_is_deploy_repo
from app.message_audience import is_operator_visible, route_event
from app.models import Alert, AlertSeverity, Project, _utcnow


CORE_ALERT_DOMAIN = "core"
DOMAIN_RELEASE_LIFECYCLE = "release_lifecycle"
DOMAIN_SR_QUALITY = "sr_quality"
DOMAIN_WARM_POOL = "warm_pool"
DOMAIN_RESEARCH_DIRECTIONS = "research_directions"
DOMAIN_ROLLUP_LTS = "rollup_lts"
DOMAIN_MIGRATIONS = "migrations"
DOMAIN_SELF_IMPROVEMENT = "self_improvement"

ROUTE_ALERT = "alert"
ROUTE_TIMELINE = "timeline"
ROUTE_HEALTH = "health"
ROUTE_SUPPRESS = "suppress"


ALERT_DOMAIN_DEFINITIONS: dict[str, dict[str, str]] = {
    CORE_ALERT_DOMAIN: {
        "label": "Core",
        "description": "Portable operator incidents that apply to every project.",
    },
    DOMAIN_RELEASE_LIFECYCLE: {
        "label": "Release Lifecycle",
        "description": "Version qualification, promotion, serving, and release stalls.",
    },
    DOMAIN_SR_QUALITY: {
        "label": "Self-review Quality",
        "description": "Self-review catch-rate, lone-checker, and review quality signals.",
    },
    DOMAIN_WARM_POOL: {
        "label": "Warm Pool",
        "description": "Warm VM pool payoff, cost guard, and pool capacity decisions.",
    },
    DOMAIN_RESEARCH_DIRECTIONS: {
        "label": "Research Directions",
        "description": "Strategic directions, staged work, diagnosis, and research loops.",
    },
    DOMAIN_ROLLUP_LTS: {
        "label": "Rollup / LTS",
        "description": "Staging rollups, promotion-target backmerges, and LTS drift.",
    },
    DOMAIN_MIGRATIONS: {
        "label": "Migrations",
        "description": "Schema migration serialization and migration-chain safety.",
    },
    DOMAIN_SELF_IMPROVEMENT: {
        "label": "Self-improvement",
        "description": "Blueprint self-improvement and strategic payload health.",
    },
}


# Transitional compatibility: Blueprint's own self-repo can keep receiving the
# domain alerts it already relies on without enabling each one manually. Other
# projects must opt into these domains through build_config.alert_domains.
SELF_REPO_DEFAULT_DOMAINS: frozenset[str] = frozenset(
    {
        DOMAIN_RELEASE_LIFECYCLE,
        DOMAIN_SR_QUALITY,
        DOMAIN_WARM_POOL,
        DOMAIN_RESEARCH_DIRECTIONS,
        DOMAIN_ROLLUP_LTS,
        DOMAIN_MIGRATIONS,
        DOMAIN_SELF_IMPROVEMENT,
    }
)


@dataclass(frozen=True)
class AlertEventPolicy:
    event_type: str
    domain: str = CORE_ALERT_DOMAIN
    route: str = ROUTE_ALERT
    allow_info: bool = False
    action_required: bool = True
    audience: str | None = None


@dataclass(frozen=True)
class AlertDecision:
    emit: bool
    event_type: str
    domain: str
    route: str
    reason: str = ""


ALERT_EVENT_POLICIES: dict[str, AlertEventPolicy] = {
    # Core portable alerts.
    "task.failed": AlertEventPolicy("task.failed"),
    "task.blocked": AlertEventPolicy("task.blocked"),
    "approval.needed": AlertEventPolicy("approval.needed"),
    "cost.ceiling_reached": AlertEventPolicy("cost.ceiling_reached"),
    "cost.spend_hold": AlertEventPolicy("cost.spend_hold"),
    "capacity.quota_exhausted": AlertEventPolicy("capacity.quota_exhausted"),
    "capacity.all_accounts_blocked": AlertEventPolicy("capacity.all_accounts_blocked"),
    # Self-hosted fleet runner health incidents: portable operator incidents
    # (any project can enroll operator hardware), always-on ``core`` domain.
    # ``runner.deactivated`` fires when the runner misses its heartbeat window
    # (offline / dead); ``runner.degraded`` fires when the runner heartbeats
    # normally but fails every job it claims (connected but broken).
    "runner.deactivated": AlertEventPolicy("runner.deactivated"),
    "runner.degraded": AlertEventPolicy("runner.degraded"),
    "deployment.halted": AlertEventPolicy("deployment.halted"),
    "deployment.rollback_recommended": AlertEventPolicy(
        "deployment.rollback_recommended"
    ),
    "deployment.rollback_detected": AlertEventPolicy("deployment.rollback_detected"),
    "deployment.auto_pause": AlertEventPolicy("deployment.auto_pause"),
    "deployment.forced_resume": AlertEventPolicy("deployment.forced_resume"),
    "deployment.flapping_resume": AlertEventPolicy("deployment.flapping_resume"),
    "deployment.auto_resume": AlertEventPolicy(
        "deployment.auto_resume", route=ROUTE_TIMELINE, action_required=False
    ),
    "deployment.halted_recovered": AlertEventPolicy(
        "deployment.halted_recovered", route=ROUTE_TIMELINE, action_required=False
    ),
    "branch.unsafe_config": AlertEventPolicy("branch.unsafe_config"),
    "branch.missing_config": AlertEventPolicy("branch.missing_config"),
    "branch.promotion_target_landing": AlertEventPolicy(
        "branch.promotion_target_landing"
    ),
    "merge.unverified": AlertEventPolicy("merge.unverified"),
    "review.escalated": AlertEventPolicy("review.escalated"),
    "review.fail_open": AlertEventPolicy("review.fail_open"),
    # Reusable opt-in domains.
    "release.calcifying": AlertEventPolicy(
        "release.calcifying", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.ci_unresolved": AlertEventPolicy(
        "release.ci_unresolved", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.build_stalled": AlertEventPolicy(
        "release.build_stalled", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.stabilize_stalled": AlertEventPolicy(
        "release.stabilize_stalled", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.promotion_deploy_failed": AlertEventPolicy(
        "release.promotion_deploy_failed", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.promotion_auto_merge_stalled": AlertEventPolicy(
        "release.promotion_auto_merge_stalled", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.managed_check_wait_stalled": AlertEventPolicy(
        "release.managed_check_wait_stalled", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.qualification_timeout": AlertEventPolicy(
        "release.qualification_timeout", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "release.qualification_crash_loop": AlertEventPolicy(
        "release.qualification_crash_loop", domain=DOMAIN_RELEASE_LIFECYCLE
    ),
    "sr.catch_rate_below_target": AlertEventPolicy(
        "sr.catch_rate_below_target", domain=DOMAIN_SR_QUALITY
    ),
    "sr.lone_checker": AlertEventPolicy("sr.lone_checker", domain=DOMAIN_SR_QUALITY),
    "sr.signal_stale": AlertEventPolicy(
        "sr.signal_stale", domain=DOMAIN_SR_QUALITY, route=ROUTE_HEALTH
    ),
    "warm_pool.payoff": AlertEventPolicy(
        "warm_pool.payoff", domain=DOMAIN_WARM_POOL, allow_info=True
    ),
    "warm_pool.cost_guard": AlertEventPolicy(
        "warm_pool.cost_guard", domain=DOMAIN_WARM_POOL
    ),
    "research.stale_staged_direction": AlertEventPolicy(
        "research.stale_staged_direction", domain=DOMAIN_RESEARCH_DIRECTIONS
    ),
    "research.direction_auto_completed": AlertEventPolicy(
        "research.direction_auto_completed",
        domain=DOMAIN_RESEARCH_DIRECTIONS,
        route=ROUTE_TIMELINE,
        action_required=False,
    ),
    "research.strategic_issue": AlertEventPolicy(
        "research.strategic_issue", domain=DOMAIN_RESEARCH_DIRECTIONS
    ),
    "rollup.backmerge_blocked": AlertEventPolicy(
        "rollup.backmerge_blocked", domain=DOMAIN_ROLLUP_LTS
    ),
    "rollup.pr_blocked": AlertEventPolicy(
        "rollup.pr_blocked", domain=DOMAIN_ROLLUP_LTS
    ),
    "migration.slot_contention": AlertEventPolicy(
        "migration.slot_contention", domain=DOMAIN_MIGRATIONS
    ),
    "migration.chain_blocked": AlertEventPolicy(
        "migration.chain_blocked", domain=DOMAIN_MIGRATIONS
    ),
    "self_improvement.payload_too_large": AlertEventPolicy(
        "self_improvement.payload_too_large", domain=DOMAIN_SELF_IMPROVEMENT
    ),
    "cost.unpriced_models": AlertEventPolicy(
        "cost.unpriced_models", route=ROUTE_HEALTH, action_required=False
    ),
    "efficiency.self_repair_burn_share": AlertEventPolicy(
        "efficiency.self_repair_burn_share",
        route=ROUTE_HEALTH,
        action_required=False,
    ),
    "efficiency.provider_routing_verdict": AlertEventPolicy(
        "efficiency.provider_routing_verdict",
    ),
}


def get_alert_event_policy(event_type: str | None) -> AlertEventPolicy:
    """Return a policy, defaulting unknown events to core alert behavior."""

    normalized = (event_type or "").strip()
    if not normalized:
        normalized = "legacy.alert"
    return ALERT_EVENT_POLICIES.get(normalized) or AlertEventPolicy(normalized)


def _configured_alert_domains(project: Project | None) -> dict[str, dict[str, Any]]:
    build_config = (getattr(project, "build_config", None) or {}) if project else {}
    raw = build_config.get("alert_domains") or {}
    configured: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for name in raw:
            domain = str(name)
            configured[domain] = {"enabled": True}
        return configured
    if not isinstance(raw, dict):
        return configured
    for name, value in raw.items():
        domain = str(name)
        if isinstance(value, bool):
            configured[domain] = {"enabled": value}
        elif isinstance(value, dict):
            configured[domain] = {"enabled": bool(value.get("enabled", False))}
        elif value is None:
            continue
        else:
            configured[domain] = {"enabled": bool(value)}
    return configured


def project_alert_domain_enabled(project: Project | None, domain: str) -> bool:
    if domain == CORE_ALERT_DOMAIN:
        return True
    configured = _configured_alert_domains(project)
    if domain in configured:
        return bool(configured[domain].get("enabled", False))
    return project_is_deploy_repo(project) and domain in SELF_REPO_DEFAULT_DOMAINS


def should_emit_project_alert(
    project: Project | None,
    event_type: str,
    *,
    severity: AlertSeverity | str = AlertSeverity.WARNING,
) -> AlertDecision:
    policy = get_alert_event_policy(event_type)
    severity_value = severity.value if isinstance(severity, AlertSeverity) else str(severity)

    # Audience gate: non-operator audiences suppress the alert (fail-closed).
    # This is the alert-side analogue of the ledger-side audience check
    # in notification_dispatch.post_event_message — both consult the same
    # shared registry in message_audience.
    audience = policy.audience or route_event(event_type)
    if not is_operator_visible(audience):
        return AlertDecision(
            False,
            policy.event_type,
            policy.domain,
            policy.route,
            reason=f"audience:{audience}",
        )

    if policy.route != ROUTE_ALERT:
        return AlertDecision(
            False,
            policy.event_type,
            policy.domain,
            policy.route,
            reason=f"route:{policy.route}",
        )
    if severity_value == AlertSeverity.INFO.value and not policy.allow_info:
        return AlertDecision(
            False,
            policy.event_type,
            policy.domain,
            policy.route,
            reason="info_severity",
        )
    if not project_alert_domain_enabled(project, policy.domain):
        return AlertDecision(
            False,
            policy.event_type,
            policy.domain,
            policy.route,
            reason=f"domain_disabled:{policy.domain}",
        )
    return AlertDecision(True, policy.event_type, policy.domain, policy.route)


async def alert_policy_allows(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_type: str,
    *,
    severity: AlertSeverity | str = AlertSeverity.WARNING,
) -> AlertDecision:
    project = await session.get(Project, project_id)
    return should_emit_project_alert(project, event_type, severity=severity)


async def _find_active_deduped_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
    dedupe_key: str,
) -> Alert | None:
    return (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.dedupe_key == dedupe_key,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


def _update_existing_alert(
    alert: Alert,
    *,
    decision: AlertDecision,
    severity: AlertSeverity,
    title: str,
    message: str,
    task_id: uuid.UUID | None,
    source: str | None,
) -> Alert:
    now = _utcnow()
    alert.severity = severity
    alert.title = title
    alert.message = message
    alert.domain = decision.domain
    alert.event_type = decision.event_type
    alert.source = source
    alert.task_id = task_id or alert.task_id
    alert.occurrence_count = int(alert.occurrence_count or 1) + 1
    alert.last_seen_at = now
    return alert


async def create_project_alert_if_allowed(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_type: str,
    severity: AlertSeverity,
    title: str,
    message: str,
    task_id: uuid.UUID | None = None,
    source: str | None = None,
    dedupe_key: str | None = None,
    broadcast: bool = False,
) -> tuple[Alert | None, bool, AlertDecision]:
    """Create or update a durable project alert after applying policy.

    Returns ``(alert, created, decision)``.  ``alert`` is ``None`` when policy
    suppresses the durable alert.  Callers own transaction commit; broadcasting
    is opt-in for call sites that deliberately accept a pre-commit notification.
    """

    project = await session.get(Project, project_id)
    decision = should_emit_project_alert(project, event_type, severity=severity)
    if not decision.emit:
        return None, False, decision

    existing = (
        await _find_active_deduped_alert(session, project_id, dedupe_key)
        if dedupe_key
        else None
    )

    now = _utcnow()
    if existing is not None:
        _update_existing_alert(
            existing,
            decision=decision,
            severity=severity,
            title=title,
            message=message,
            task_id=task_id,
            source=source,
        )
        session.add(existing)
        return existing, False, decision

    alert = Alert(
        project_id=project_id,
        severity=severity,
        title=title,
        message=message,
        task_id=task_id,
        domain=decision.domain,
        event_type=decision.event_type,
        source=source,
        dedupe_key=dedupe_key,
        occurrence_count=1,
        last_seen_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(alert)
            await session.flush()
    except IntegrityError:
        if not dedupe_key:
            raise
        existing = await _find_active_deduped_alert(session, project_id, dedupe_key)
        if existing is None:
            raise
        _update_existing_alert(
            existing,
            decision=decision,
            severity=severity,
            title=title,
            message=message,
            task_id=task_id,
            source=source,
        )
        session.add(existing)
        await session.flush()
        return existing, False, decision
    if broadcast:
        await broadcast_to_project(
            str(project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": severity.value,
                "title": alert.title,
                "domain": alert.domain,
                "event_type": alert.event_type,
            },
        )
    return alert, True, decision


def serialize_project_alert_domains(project: Project) -> dict[str, Any]:
    configured = _configured_alert_domains(project)
    domains = []
    for domain, meta in sorted(ALERT_DOMAIN_DEFINITIONS.items()):
        configured_value = configured.get(domain)
        default_enabled = domain == CORE_ALERT_DOMAIN or (
            project_is_deploy_repo(project) and domain in SELF_REPO_DEFAULT_DOMAINS
        )
        enabled = (
            bool(configured_value.get("enabled"))
            if configured_value is not None
            else default_enabled
        )
        domains.append(
            {
                "domain": domain,
                "label": meta["label"],
                "description": meta["description"],
                "enabled": enabled,
                "configured": configured_value is not None,
                "default_enabled": default_enabled,
                "core": domain == CORE_ALERT_DOMAIN,
            }
        )
    return {
        "project_id": str(project.id),
        "domains": domains,
    }


def apply_project_alert_domain_updates(
    project: Project, updates: dict[str, Any]
) -> dict[str, Any]:
    """Persist normalized alert-domain updates into project.build_config.

    ``updates`` maps domain name to ``{"enabled": bool}``, bool, or ``None`` to
    reset that domain to its default.  Core alerts cannot be disabled.
    """

    normalized: dict[str, dict[str, bool] | None] = {}
    for raw_domain, raw_value in updates.items():
        domain = str(raw_domain)
        if domain not in ALERT_DOMAIN_DEFINITIONS:
            raise ValueError(f"Unknown alert domain: {domain}")
        if domain == CORE_ALERT_DOMAIN:
            raise ValueError("Core alert domain cannot be configured")
        if raw_value is None:
            normalized[domain] = None
        elif isinstance(raw_value, bool):
            normalized[domain] = {"enabled": raw_value}
        elif isinstance(raw_value, dict) and isinstance(raw_value.get("enabled"), bool):
            normalized[domain] = {"enabled": raw_value["enabled"]}
        else:
            raise ValueError(
                f"Alert domain '{domain}' must be a boolean, null, or object with boolean enabled"
            )

    build_config = dict(project.build_config or {})
    existing = _configured_alert_domains(project)
    for domain, value in normalized.items():
        if value is None:
            existing.pop(domain, None)
        else:
            existing[domain] = value
    if existing:
        build_config["alert_domains"] = {
            domain: {"enabled": bool(value.get("enabled", False))}
            for domain, value in sorted(existing.items())
        }
    else:
        build_config.pop("alert_domains", None)
    project.build_config = build_config
    return serialize_project_alert_domains(project)
