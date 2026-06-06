"""Quota-scope classification for GCP provisioning errors.

Not every retryable provisioning error is evidence about the zone it happened
in. A CPUS_ALL_REGIONS quota exhaustion is a PROJECT-GLOBAL condition (the
error text literally says "globally") — no individual zone is unhealthy, and
charging it to zones marks EVERY enabled zone failed on a single quota event,
stacking per-zone exponential cooldowns that outlive the quota event (the
cross-zone painting also defeats the per-zone debounce). Regional quotas
(e.g. CPUS / IN_USE_ADDRESSES "in region X") affect every zone in that region
but say nothing about other regions. Only genuine zone stockouts
(ZONE_RESOURCE_POOL_EXHAUSTED / "does not have enough resources") are
evidence about a specific zone.

This module owns the classification (``classify_provision_error_scope``) and
the project-global / per-region quota cooldown state. ``app.compute`` imports
these symbols — so they stay reachable as ``app.compute.*``, and the state
dicts are only ever mutated in place, which keeps those aliases live — and
honours the cooldowns in its ``_zone_blocked_until`` / ``any_zone_available``
/ ``soonest_zone_recovery``, surfacing them via ``get_zone_health_summary``.
Extracted from ``compute.py`` to keep the provisioning substrate within its
module-size guardrail.

Locking: quota state shares ``app.compute._zone_lock`` — a single lock for
ALL zone state, because quota cooldowns are read together with zone-health
cooldowns under one acquisition (``_zone_blocked_until``). The lock is
resolved at call time via ``_zone_state_lock()``: ``compute`` imports this
module at load (a top-level back-import would be circular), and call-time
resolution also honours tests that patch ``app.compute._zone_lock``.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

QUOTA_SCOPE_GLOBAL = "global"
QUOTA_SCOPE_REGIONAL = "regional"
QUOTA_SCOPE_ZONAL = "zonal"

# Quota metrics that are project-global regardless of surrounding wording.
_GLOBAL_QUOTA_MARKERS = ("cpus_all_regions", "gpus_all_regions")
# Error markers that are decisively zone-local (zone stockout / shortage).
_ZONAL_ERROR_MARKERS = (
    "zone_resource_pool_exhausted",
    "does not have enough resources",
)

GLOBAL_QUOTA_COOLDOWN_SECONDS = int(
    os.getenv("GLOBAL_QUOTA_COOLDOWN_SECONDS", "300")
)  # base for project-level quota backoff
REGION_QUOTA_COOLDOWN_SECONDS = int(
    os.getenv("REGION_QUOTA_COOLDOWN_SECONDS", "300")
)  # base for per-region quota backoff
_QUOTA_COOLDOWN_CAP_SECONDS = 1800  # same 30-min cap as zone cooldowns
_QUOTA_ALERT_DEDUP_SECONDS = 1800  # at most one operator alert per 30 min

# Project-global quota cooldown state (guarded by compute._zone_lock). Keys:
#   cooldown_expires_at: datetime, consecutive: int, last_event: datetime,
#   last_error: str, last_alert_at: datetime
# In-memory only (not persisted across restarts): the cooldown is short-lived
# and a restart simply re-detects the condition on the first failed insert.
_global_quota_state: dict = {}
# Per-region quota cooldown state (guarded by compute._zone_lock):
# region -> same shape.
_region_quota_state: dict[str, dict] = {}


def _zone_region(zone: str) -> str:
    """``us-central1-f`` → ``us-central1``."""
    return "-".join(zone.split("-")[:-1])


def _zone_state_lock():
    """The shared zone-state lock (see module docstring on why call-time)."""
    from app.compute import _zone_lock

    return _zone_lock


def classify_provision_error_scope(err_str: str) -> str:
    """Classify the blast radius of a retryable provisioning error.

    Returns ``QUOTA_SCOPE_GLOBAL`` for project-wide quota exhaustion
    (CPUS_ALL_REGIONS / a quota error that says "globally"),
    ``QUOTA_SCOPE_REGIONAL`` for region-scoped quota errors, and
    ``QUOTA_SCOPE_ZONAL`` for everything else (zone stockouts, missing
    subnets, …) — i.e. errors where marking the attempted zone is correct.
    """
    low = (err_str or "").lower()
    if any(m in low for m in _GLOBAL_QUOTA_MARKERS):
        return QUOTA_SCOPE_GLOBAL
    if any(m in low for m in _ZONAL_ERROR_MARKERS):
        return QUOTA_SCOPE_ZONAL
    if "quota" in low:
        # GCP quota wording: global quotas say "Limit: N globally.", regional
        # quotas say "Limit: N in region <r>.". Default unscoped quota errors
        # to regional — the conservative middle ground (never paints zones in
        # other regions, never silently pauses the whole project).
        return QUOTA_SCOPE_GLOBAL if "globally" in low else QUOTA_SCOPE_REGIONAL
    return QUOTA_SCOPE_ZONAL


def _quota_backoff_seconds(consecutive: int, base: int) -> int:
    """Exponential backoff for quota cooldowns: base → 2x → 4x …, 30-min cap."""
    return min(base * (2 ** max(consecutive - 1, 0)), _QUOTA_COOLDOWN_CAP_SECONDS)


def _post_global_quota_alert(
    err_str: str, cooldown_secs: int, consecutive: int
) -> None:
    """Operator alert for project-global quota exhaustion.

    Structured ERROR log + best-effort webhook (mirrors
    ``shared_state._post_pipeline_alert``). Runs from the provisioning worker
    thread (no event loop) or from async callers — handles both. Failures here
    must never break provisioning.
    """
    title = "GCP project quota exhausted — provisioning paused project-wide"
    message = (
        f"Provisioning hit a project-global quota error (event #{consecutive}). "
        f"Pausing provisioning in ALL zones for {cooldown_secs}s instead of "
        f"marking zones unhealthy — no zone is at fault. Wait for running VMs "
        f"to wind down or raise the quota (e.g. CPUS_ALL_REGIONS). "
        f"Error: {err_str[:300]}"
    )
    logger.error(
        title,
        extra={
            "alert": True,
            "type": "quota_exhausted",
            "scope": QUOTA_SCOPE_GLOBAL,
            "cooldown_seconds": cooldown_secs,
            "consecutive": consecutive,
        },
    )
    try:
        from app.notification import _send_webhook_notification

        coro = _send_webhook_notification("GLOBAL_QUOTA_EXHAUSTED", title, message)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(coro)  # fire-and-forget by design
        else:
            # Provisioning runs in a worker thread (asyncio.to_thread) — no
            # running loop here; run the one-shot webhook synchronously
            # (_send_webhook_notification swallows its own errors, 5s timeout).
            asyncio.run(coro)
    except Exception as e:
        logger.warning(f"[QUOTA] global-quota alert webhook skipped: {e}")


def _record_global_quota_exhaustion(err_str: str) -> int:
    """Arm/extend the PROJECT-level quota backoff. Returns the cooldown seconds.

    Deliberately marks NO zones: a global quota event says nothing about zone
    health. ``any_zone_available()`` / ``soonest_zone_recovery()`` honour this
    cooldown, so dispatch pauses project-wide and resumes when it expires.
    Debounced like zone failures: parallel provision attempts failing on the
    same quota event within the base window don't escalate the backoff.
    """
    with _zone_state_lock():
        now = datetime.now(timezone.utc)
        state = _global_quota_state
        last_event = state.get("last_event")
        within_debounce = (
            last_event is not None
            and (now - last_event).total_seconds() < GLOBAL_QUOTA_COOLDOWN_SECONDS
        )
        consecutive = state.get("consecutive", 0)
        if not within_debounce:
            consecutive += 1
        cooldown_secs = _quota_backoff_seconds(
            consecutive, GLOBAL_QUOTA_COOLDOWN_SECONDS
        )
        state["last_event"] = now
        state["consecutive"] = consecutive
        state["last_error"] = err_str[:300]
        state["cooldown_expires_at"] = now + timedelta(seconds=cooldown_secs)
        last_alert = state.get("last_alert_at")
        should_alert = (
            last_alert is None
            or (now - last_alert).total_seconds() >= _QUOTA_ALERT_DEDUP_SECONDS
        )
        if should_alert:
            state["last_alert_at"] = now
    if should_alert:
        # Outside the lock — the webhook can take up to 5s.
        _post_global_quota_alert(err_str, cooldown_secs, consecutive)
    return cooldown_secs


def _record_region_quota_exhaustion(region: str, err_str: str) -> int:
    """Arm/extend a REGION-level quota backoff. Returns the cooldown seconds.

    Marks no individual zone — every zone in ``region`` is equally blocked by
    the regional quota, and zones in other regions are unaffected.
    """
    with _zone_state_lock():
        now = datetime.now(timezone.utc)
        state = _region_quota_state.setdefault(region, {})
        last_event = state.get("last_event")
        within_debounce = (
            last_event is not None
            and (now - last_event).total_seconds() < REGION_QUOTA_COOLDOWN_SECONDS
        )
        consecutive = state.get("consecutive", 0)
        if not within_debounce:
            consecutive += 1
        cooldown_secs = _quota_backoff_seconds(
            consecutive, REGION_QUOTA_COOLDOWN_SECONDS
        )
        state["last_event"] = now
        state["consecutive"] = consecutive
        state["last_error"] = err_str[:300]
        state["cooldown_expires_at"] = now + timedelta(seconds=cooldown_secs)
    return cooldown_secs


def _clear_quota_cooldowns_after_success(zone: str) -> None:
    """A successful insert proves project-global headroom (and regional headroom
    for this zone's region) — drop those cooldowns so recovery is immediate.
    Caller must hold ``compute._zone_lock``."""
    if _global_quota_state:
        _global_quota_state.clear()
    _region_quota_state.pop(_zone_region(zone), None)


def get_quota_cooldowns() -> dict:
    """Snapshot of active project-global / per-region quota cooldowns
    (for health endpoints and operator tooling)."""
    now = datetime.now(timezone.utc)
    with _zone_state_lock():
        global_cd = _global_quota_state.get("cooldown_expires_at")
        global_until = global_cd.isoformat() if global_cd and now < global_cd else None
        regions = {
            region: state["cooldown_expires_at"].isoformat()
            for region, state in _region_quota_state.items()
            if state.get("cooldown_expires_at") and now < state["cooldown_expires_at"]
        }
    return {
        "global_quota_cooldown_until": global_until,
        "region_quota_cooldowns": regions,
    }
