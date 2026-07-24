import pytest
from unittest.mock import AsyncMock

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.alert_policy import (
    ALERT_EVENT_POLICIES,
    DOMAIN_RELEASE_LIFECYCLE,
    DOMAIN_WARM_POOL,
    ROUTE_HEALTH,
    apply_project_alert_domain_updates,
    create_project_alert_if_allowed,
    should_emit_project_alert,
)
from app.models import Alert, AlertSeverity
from testing.factories import make_membership, make_project


def _domain_row(payload: dict, domain: str) -> dict:
    return next(row for row in payload["domains"] if row["domain"] == domain)


def test_core_alerts_are_enabled_for_all_projects():
    project = make_project(is_self_repo=False)

    decision = should_emit_project_alert(
        project,
        "cost.ceiling_reached",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is True
    assert decision.domain == "core"


def test_custom_domain_alerts_require_project_enablement():
    project = make_project(is_self_repo=False)

    decision = should_emit_project_alert(
        project,
        "release.calcifying",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is False
    assert decision.reason == "domain_disabled:release_lifecycle"


def test_build_config_can_enable_reusable_alert_domain():
    project = make_project(
        is_self_repo=False,
        build_config={"alert_domains": {DOMAIN_RELEASE_LIFECYCLE: {"enabled": True}}},
    )

    decision = should_emit_project_alert(
        project,
        "release.calcifying",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is True
    assert decision.domain == DOMAIN_RELEASE_LIFECYCLE


def test_self_repo_keeps_existing_domain_alert_defaults():
    project = make_project(is_self_repo=True)

    decision = should_emit_project_alert(
        project,
        "release.calcifying",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is True
    assert decision.domain == DOMAIN_RELEASE_LIFECYCLE


def test_deploy_identity_grants_default_domains_to_matching_repo(monkeypatch):
    # A project that is NOT flagged is_self_repo but whose GitHub repo IS the
    # stamped deploy identity gets the self-repo default domains — classification
    # follows deploy identity, not the per-project boolean.
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    project = make_project(
        is_self_repo=False, github_owner="acme", github_repo="widget"
    )

    decision = should_emit_project_alert(
        project,
        "release.calcifying",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is True
    assert decision.domain == DOMAIN_RELEASE_LIFECYCLE


def test_deploy_identity_denies_default_domains_to_misset_self_repo(monkeypatch):
    # is_self_repo misset True but the repo is NOT the deploy identity → the
    # self-repo default domains are withheld (identity is authoritative).
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    project = make_project(
        is_self_repo=True, github_owner="other", github_repo="proj"
    )

    decision = should_emit_project_alert(
        project,
        "release.calcifying",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is False
    assert decision.reason == f"domain_disabled:{DOMAIN_RELEASE_LIFECYCLE}"


def test_non_alert_routes_do_not_create_durable_alerts():
    project = make_project(is_self_repo=True)

    decision = should_emit_project_alert(
        project,
        "cost.unpriced_models",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is False
    assert decision.route == ROUTE_HEALTH
    assert decision.reason == "route:health"


def test_info_severity_is_suppressed_for_durable_alerts():
    project = make_project(is_self_repo=True)

    decision = should_emit_project_alert(
        project,
        "release.calcifying",
        severity=AlertSeverity.INFO,
    )

    assert decision.emit is False
    assert decision.reason == "info_severity"
    assert decision.domain == DOMAIN_RELEASE_LIFECYCLE


def test_audience_gate_suppresses_strategist_internal_events():
    """Strategist-internal events are suppressed regardless of domain enablement."""
    project = make_project(is_self_repo=True)

    decision = should_emit_project_alert(
        project,
        "warm_pool.payoff",
        severity=AlertSeverity.WARNING,
    )

    assert decision.emit is False
    assert decision.reason == "audience:strategist-internal"


def test_apply_project_alert_domain_updates_round_trips_build_config():
    project = make_project(is_self_repo=False)

    payload = apply_project_alert_domain_updates(
        project,
        {DOMAIN_WARM_POOL: {"enabled": True}},
    )

    assert project.build_config == {
        "alert_domains": {DOMAIN_WARM_POOL: {"enabled": True}}
    }
    assert _domain_row(payload, DOMAIN_WARM_POOL)["enabled"] is True
    assert _domain_row(payload, DOMAIN_WARM_POOL)["configured"] is True

    payload = apply_project_alert_domain_updates(project, {DOMAIN_WARM_POOL: None})
    assert project.build_config == {}
    assert _domain_row(payload, DOMAIN_WARM_POOL)["enabled"] is False
    assert _domain_row(payload, DOMAIN_WARM_POOL)["configured"] is False


def test_core_alert_domain_cannot_be_disabled():
    project = make_project()

    with pytest.raises(ValueError, match="Core alert domain"):
        apply_project_alert_domain_updates(project, {"core": {"enabled": False}})


@pytest.mark.asyncio
async def test_create_project_alert_if_allowed_suppresses_disabled_domain(
    db_session, mocker
):
    broadcast = mocker.patch(
        "app.alert_policy.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project(is_self_repo=False)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    alert, created, decision = await create_project_alert_if_allowed(
        db_session,
        project_id=project.id,
        event_type="release.calcifying",
        severity=AlertSeverity.WARNING,
        title="Release calcifying",
        message="Version release is calcifying.",
        source="test",
        dedupe_key=f"release.calcifying:{project.id}",
        broadcast=True,
    )

    assert alert is None
    assert created is False
    assert decision.reason == "domain_disabled:release_lifecycle"
    broadcast.assert_not_called()
    rows = (await db_session.execute(select(Alert))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_create_project_alert_if_allowed_dedups_and_counts(db_session, mocker):
    broadcast = mocker.patch(
        "app.alert_policy.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    kwargs = dict(
        project_id=project.id,
        event_type="runner.deactivated",
        severity=AlertSeverity.WARNING,
        title="Runner deactivated",
        message="Runner is offline.",
        source="test",
        dedupe_key=f"runner.deactivated:{project.id}",
        broadcast=True,
    )
    first, created, decision = await create_project_alert_if_allowed(
        db_session, **kwargs
    )
    second, created_again, _ = await create_project_alert_if_allowed(
        db_session, **{**kwargs, "message": "Still offline."}
    )

    assert decision.emit is True
    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert second.domain == "core"
    assert second.event_type == "runner.deactivated"
    assert second.occurrence_count == 2
    assert second.last_seen_at is not None
    assert second.message == "Still offline."
    broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_dedupe_key_is_unique_until_dismissed(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="First",
            message="first",
            dedupe_key="same-active-key",
        )
    )
    db_session.add(
        Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="Second",
            message="second",
            dedupe_key="same-active-key",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_alert_domains_admin_endpoint_round_trip(client, db_session):
    project = make_project(is_self_repo=False)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id, role="owner"))
    await db_session.commit()

    get_resp = await client.get(f"/api/admin/projects/{project.id}/alert-domains")
    assert get_resp.status_code == 200, get_resp.text
    assert _domain_row(get_resp.json(), DOMAIN_WARM_POOL)["enabled"] is False

    patch_resp = await client.patch(
        f"/api/admin/projects/{project.id}/alert-domains",
        json={"domains": {DOMAIN_WARM_POOL: {"enabled": True}}},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert _domain_row(patch_resp.json(), DOMAIN_WARM_POOL)["enabled"] is True

    await db_session.refresh(project)
    assert project.build_config["alert_domains"][DOMAIN_WARM_POOL] == {
        "enabled": True
    }


@pytest.mark.asyncio
async def test_alert_domains_admin_endpoint_rejects_unknown_domain(client, db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id, role="owner"))
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/alert-domains",
        json={"domains": {"does_not_exist": {"enabled": True}}},
    )

    assert resp.status_code == 422


def test_wake_delivery_stalled_is_registered_in_alert_event_policies():
    """deployment.wake_delivery_stalled must be in ALERT_EVENT_POLICIES so it is
    unambiguously routed as a core alert rather than relying on the fallback path."""
    assert "deployment.wake_delivery_stalled" in ALERT_EVENT_POLICIES


def test_wake_delivery_stalled_emits_for_core_project():
    """deployment.wake_delivery_stalled must be emittable for any project (core domain)."""
    project = make_project(is_self_repo=False)
    decision = should_emit_project_alert(project, "deployment.wake_delivery_stalled")
    assert decision.emit is True, f"expected emit=True but got reason={decision.reason}"
