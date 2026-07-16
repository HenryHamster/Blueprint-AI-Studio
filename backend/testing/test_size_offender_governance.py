"""Tests for chronic module-size-offender governance.

Covers:
- ``_is_chronic_offender`` threshold logic (window and growth triggers)
- ``_parse_git_diff_output`` diff parsing
- ``_emit_size_violation_alerts`` operator-alert suppression
- Repeated chronic and first-time violations remain strategist-internal
- Acceptance criterion: ``test_chronic_offender_escalates_not_silently_bumped``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import select

from app import hotfix_backmerge as hb
from app.models import Alert, Project
from app.size_limit_policy import (
    CHRONIC_WINDOW_DAYS,
    BumpRecord,
    _is_chronic_offender,
    _parse_git_diff_output,
    classify_breach,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(old_cap: int, new_cap: int, days_ago: int) -> BumpRecord:
    date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return BumpRecord(
        commit_sha=f"sha{days_ago}",
        commit_date=date,
        old_cap=old_cap,
        new_cap=new_cap,
    )


async def _make_project(db_session) -> Project:
    project = Project(
        name="GovTest",
        user_id="gov-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _alerts_for(db_session, project_id) -> list[Alert]:
    return (
        (await db_session.execute(select(Alert).where(Alert.project_id == project_id)))
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# _is_chronic_offender — unit tests (no DB, no subprocess)
# ---------------------------------------------------------------------------


def test_chronic_below_threshold_returns_false():
    """Fewer than CHRONIC_BUMP_THRESHOLD bumps → not chronic regardless of growth."""
    history = [
        _record(1000, 1250, 30),
        _record(1250, 1500, 20),
    ]
    assert not _is_chronic_offender(history)


def test_chronic_threshold_met_within_window():
    """Exactly CHRONIC_BUMP_THRESHOLD recent bumps → chronic (window trigger)."""
    history = [
        _record(1000, 1250, CHRONIC_WINDOW_DAYS - 60),
        _record(1250, 1500, CHRONIC_WINDOW_DAYS - 90),
        _record(1500, 1750, 20),
    ]
    assert _is_chronic_offender(history)


def test_chronic_all_outside_window_low_growth_not_chronic():
    """3+ bumps but all older than CHRONIC_WINDOW_DAYS AND growth < 30% → not chronic."""
    history = [
        _record(1000, 1080, CHRONIC_WINDOW_DAYS + 100),
        _record(1080, 1160, CHRONIC_WINDOW_DAYS + 60),
        _record(1160, 1260, CHRONIC_WINDOW_DAYS + 10),
    ]
    # growth = (1260 - 1000) / 1000 = 26% < 30%
    assert not _is_chronic_offender(history)


def test_chronic_growth_trigger_fires_with_3_bumps():
    """3+ bumps outside window but cumulative growth >= 30% → chronic (growth trigger)."""
    history = [
        _record(1000, 1100, CHRONIC_WINDOW_DAYS + 100),
        _record(1100, 1200, CHRONIC_WINDOW_DAYS + 60),
        _record(1200, 1350, CHRONIC_WINDOW_DAYS + 10),
    ]
    # growth = (1350 - 1000) / 1000 = 35% >= 30%
    assert _is_chronic_offender(history)


def test_chronic_growth_trigger_needs_3_bumps():
    """Only 2 bumps even at > 30% growth → not chronic (growth trigger requires threshold)."""
    history = [
        _record(1000, 1150, CHRONIC_WINDOW_DAYS + 100),
        _record(1150, 1450, CHRONIC_WINDOW_DAYS + 10),
    ]
    # growth = (1450 - 1000) / 1000 = 45% but only 2 bumps
    assert not _is_chronic_offender(history)


def test_chronic_empty_history_not_chronic():
    assert not _is_chronic_offender([])


# ---------------------------------------------------------------------------
# _parse_git_diff_output — unit tests (fixture diff text)
# ---------------------------------------------------------------------------

_FIXTURE_GIT_LOG = """\
COMMIT aaa111 2024-01-10T10:00:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
index 000..111
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -10,7 +10,7 @@
   "backend": {
-    "reaper_sweeps.py": 3500,
+    "reaper_sweeps.py": 4050,
   }

COMMIT bbb222 2024-03-15T12:00:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
index 111..222
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -10,7 +10,7 @@
   "backend": {
-    "reaper_sweeps.py": 4050,
+    "reaper_sweeps.py": 4700,
   }
"""


def test_parse_git_diff_extracts_bumps():
    records = _parse_git_diff_output(_FIXTURE_GIT_LOG, "reaper_sweeps.py")
    assert len(records) == 2
    assert records[0]["old_cap"] == 3500
    assert records[0]["new_cap"] == 4050
    assert records[1]["old_cap"] == 4050
    assert records[1]["new_cap"] == 4700
    # sorted oldest first
    assert records[0]["commit_date"] < records[1]["commit_date"]


def test_parse_git_diff_ignores_other_keys():
    records = _parse_git_diff_output(_FIXTURE_GIT_LOG, "orchestrator.py")
    assert records == []


def test_parse_git_diff_ignores_decreases():
    log_with_decrease = """\
COMMIT ccc333 2024-05-01T09:00:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -5,7 +5,7 @@
-    "tiny_module.py": 5000,
+    "tiny_module.py": 100,
"""
    records = _parse_git_diff_output(log_with_decrease, "tiny_module.py")
    # Decrease (5000 → 100) is NOT a bump; should be excluded.
    assert records == []


def test_parse_git_diff_empty_output():
    assert _parse_git_diff_output("", "anything.py") == []


# ---------------------------------------------------------------------------
# _emit_size_violation_alerts — integration tests (DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chronic_offender_escalates_not_silently_bumped(db_session, mocker):
    """Size governance remains strategist-internal with no operator Alert rows."""
    project = await _make_project(db_session)

    # --- chronic offender: 3 recent bumps (within CHRONIC_WINDOW_DAYS) ---
    chronic_history = [
        _record(3500, 4050, CHRONIC_WINDOW_DAYS - 10),
        _record(4050, 4500, CHRONIC_WINDOW_DAYS - 60),
        _record(4500, 4700, 30),
    ]
    with patch(
        "app.hotfix_backmerge.parse_bump_history", return_value=chronic_history
    ) as parse_history:
        await hb._emit_size_violation_alerts(
            db_session,
            project.id,
            violations=[
                {
                    "section": "backend",
                    "rel": "reaper_sweeps.py",
                    "count": 3800,
                    "cap": 4100,
                }
            ],
            pr_number=999,
            trigger_desc="",
        )
    await db_session.flush()
    parse_history.assert_not_called()
    assert await _alerts_for(db_session, project.id) == []

    # --- first-time breach: empty history → WARNING ---
    with patch("app.hotfix_backmerge.is_operator_visible", return_value=True), \
         patch("app.hotfix_backmerge.parse_bump_history", return_value=[]):
        await hb._emit_size_violation_alerts(
            db_session,
            project.id,
            violations=[
                {
                    "section": "backend",
                    "rel": "orchestrator.py",
                    "count": 8000,
                    "cap": 7750,
                }
            ],
            pr_number=998,
            trigger_desc="",
        )
    await db_session.flush()

    assert await _alerts_for(db_session, project.id) == []


@pytest.mark.asyncio
async def test_chronic_alert_deduplicates(db_session, mocker):
    """Repeated chronic violations remain absent from operator Alert rows."""
    project = await _make_project(db_session)
    chronic_history = [
        _record(1000, 1200, CHRONIC_WINDOW_DAYS - 10),
        _record(1200, 1400, CHRONIC_WINDOW_DAYS - 60),
        _record(1400, 1600, 30),
    ]

    violation = [
        {"section": "backend", "rel": "big_module.py", "count": 1700, "cap": 1600}
    ]

    with patch("app.hotfix_backmerge.is_operator_visible", return_value=True), \
         patch("app.hotfix_backmerge.parse_bump_history", return_value=chronic_history):
        await hb._emit_size_violation_alerts(
            db_session, project.id, violations=violation, pr_number=10, trigger_desc=""
        )
        await db_session.flush()
        assert await _alerts_for(db_session, project.id) == []

        # Second call — should not create a duplicate.
        await hb._emit_size_violation_alerts(
            db_session, project.id, violations=violation, pr_number=11, trigger_desc=""
        )
        await db_session.flush()
        assert await _alerts_for(db_session, project.id) == []


@pytest.mark.asyncio
async def test_parse_bump_history_error_does_not_block_alert(db_session, mocker):
    """Suppression returns before history lookup, so history failures are irrelevant."""
    project = await _make_project(db_session)

    with patch("app.hotfix_backmerge.is_operator_visible", return_value=True), \
         patch(
             "app.hotfix_backmerge.parse_bump_history",
             side_effect=RuntimeError("git not available"),
         ):
        await hb._emit_size_violation_alerts(
            db_session,
            project.id,
            violations=[
                {
                    "section": "backend",
                    "rel": "orchestrator.py",
                    "count": 8000,
                    "cap": 7750,
                }
            ],
            pr_number=42,
            trigger_desc="",
        )
    await db_session.flush()

    assert await _alerts_for(db_session, project.id) == []


# ---------------------------------------------------------------------------
# classify_breach — unit tests (no DB, no subprocess)
# ---------------------------------------------------------------------------


def test_classify_breach_chronic_returns_no_new_limit(mocker):
    """classify_breach for a chronic offender must return is_chronic=True, suggested_new_limit=None."""
    chronic_history = [
        _record(1000, 1200, CHRONIC_WINDOW_DAYS - 10),
        _record(1200, 1400, CHRONIC_WINDOW_DAYS - 60),
        _record(1400, 1600, 30),
    ]
    with patch(
        "app.size_limit_policy.parse_bump_history", return_value=chronic_history
    ):
        verdict = classify_breach("big_module.py", 1720)

    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is None
    assert verdict["bump_count"] == 3
    assert "decomposition" in verdict["reason"].lower()


def test_classify_breach_routine_returns_new_limit(mocker):
    """classify_breach for a routine breach (no/few prior bumps) must return a new limit."""
    with patch("app.size_limit_policy.parse_bump_history", return_value=[]):
        verdict = classify_breach("new_module.py", 1720)

    assert verdict["is_chronic"] is False
    assert verdict["suggested_new_limit"] is not None
    assert verdict["suggested_new_limit"] > 1720
    assert verdict["bump_count"] == 0


def test_classify_breach_chronic_override_grants_new_limit(mocker):
    """With override_chronic=True, chronic offender still gets a suggested_new_limit."""
    chronic_history = [
        _record(1000, 1200, CHRONIC_WINDOW_DAYS - 10),
        _record(1200, 1400, CHRONIC_WINDOW_DAYS - 60),
        _record(1400, 1600, 30),
    ]
    with patch(
        "app.size_limit_policy.parse_bump_history", return_value=chronic_history
    ):
        verdict = classify_breach("big_module.py", 1720, override_chronic=True)

    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is not None
    assert verdict["suggested_new_limit"] > 1720


def test_classify_breach_git_unavailable_degrades_to_routine():
    """When git errors, classify_breach must degrade to routine (not raise)."""
    with patch(
        "app.size_limit_policy.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        verdict = classify_breach("any_module.py", 1720)

    assert verdict["is_chronic"] is False
    assert verdict["suggested_new_limit"] is not None
    assert verdict["suggested_new_limit"] > 1720
