"""Tests for chain-continuity funnel recorders and snapshot in warm_backlog_reuse.py."""

from datetime import timedelta

import pytest


def _clear_chain_events(wbr) -> None:
    wbr._chain_poll_events.clear()
    wbr._chain_claimed_events.clear()
    wbr._chain_no_pinned_events.clear()
    wbr._chain_other_vm_pr_events.clear()
    wbr._chain_doomed_events.clear()
    wbr._chain_skipped_events.clear()


def test_chain_funnel_counters_and_snapshot():
    """Each record_chain_* function increments its stage; snapshot returns accurate totals."""
    import app.warm_backlog_reuse as wbr

    _clear_chain_events(wbr)
    try:
        wbr.record_chain_poll()
        wbr.record_chain_poll()
        wbr.record_chain_claimed()
        wbr.record_chain_no_pinned()
        wbr.record_chain_other_vm_pr()
        wbr.record_chain_doomed()
        wbr.record_chain_skipped("disabled")
        wbr.record_chain_skipped("breaker_tripped")
        wbr.record_chain_skipped("disabled")

        snap = wbr.get_chain_continuity_funnel_snapshot()
        assert snap["window_hours"] == 24
        assert snap["poll_arrivals"] == 2
        assert snap["chain_claimed"] == 1
        assert snap["no_pinned_task"] == 1
        assert snap["another_vm_owns_pr"] == 1
        assert snap["doomed_vm"] == 1
        assert snap["skipped_by_reason"]["disabled"] == 2
        assert snap["skipped_by_reason"]["breaker_tripped"] == 1
        assert snap["skipped_total"] == 3
    finally:
        _clear_chain_events(wbr)


def test_chain_funnel_window_cutoff():
    """Events older than window_hours are excluded from the snapshot."""
    import app.warm_backlog_reuse as wbr
    from app.time_utils import _utcnow

    _clear_chain_events(wbr)
    try:
        old_ts = _utcnow() - timedelta(hours=25)
        wbr._chain_poll_events.append(old_ts)
        wbr._chain_claimed_events.append(old_ts)
        wbr._chain_skipped_events.append((old_ts, "disabled"))

        wbr.record_chain_poll()
        wbr.record_chain_skipped("breaker_tripped")

        snap = wbr.get_chain_continuity_funnel_snapshot(window_hours=24)
        assert snap["poll_arrivals"] == 1
        assert snap["chain_claimed"] == 0
        assert snap["skipped_by_reason"].get("disabled", 0) == 0
        assert snap["skipped_by_reason"].get("breaker_tripped", 0) == 1
    finally:
        _clear_chain_events(wbr)


def test_chain_funnel_empty_snapshot():
    """An empty funnel returns zeros for all stages."""
    import app.warm_backlog_reuse as wbr

    _clear_chain_events(wbr)
    try:
        snap = wbr.get_chain_continuity_funnel_snapshot()
        assert snap["poll_arrivals"] == 0
        assert snap["chain_claimed"] == 0
        assert snap["no_pinned_task"] == 0
        assert snap["another_vm_owns_pr"] == 0
        assert snap["doomed_vm"] == 0
        assert snap["skipped_by_reason"] == {}
        assert snap["skipped_total"] == 0
    finally:
        _clear_chain_events(wbr)


def test_chain_funnel_window_hours_param():
    """get_chain_continuity_funnel_snapshot respects a custom window_hours."""
    import app.warm_backlog_reuse as wbr
    from app.time_utils import _utcnow

    _clear_chain_events(wbr)
    try:
        two_hours_ago = _utcnow() - timedelta(hours=2)
        wbr._chain_poll_events.append(two_hours_ago)

        snap_1h = wbr.get_chain_continuity_funnel_snapshot(window_hours=1)
        snap_3h = wbr.get_chain_continuity_funnel_snapshot(window_hours=3)

        assert snap_1h["poll_arrivals"] == 0
        assert snap_3h["poll_arrivals"] == 1
    finally:
        _clear_chain_events(wbr)


@pytest.mark.asyncio
async def test_chain_funnel_endpoint_exposes_snapshot(client, db_session):
    """The warm-vm-state endpoint returns chain_continuity_funnel with expected keys."""
    import app.warm_backlog_reuse as wbr
    import app.shared_state as _shared_state

    _clear_chain_events(wbr)
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    _shared_state.vm_last_heartbeat.clear()
    try:
        wbr.record_chain_poll()
        wbr.record_chain_claimed()
        wbr.record_chain_skipped("disabled")

        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        body = resp.json()
        assert "chain_continuity_funnel" in body
        funnel = body["chain_continuity_funnel"]
        assert funnel["poll_arrivals"] >= 1
        assert funnel["chain_claimed"] >= 1
        assert funnel["skipped_by_reason"]["disabled"] >= 1
        for key in ("window_hours", "no_pinned_task", "another_vm_owns_pr", "doomed_vm", "skipped_total"):
            assert key in funnel
    finally:
        _clear_chain_events(wbr)
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)
