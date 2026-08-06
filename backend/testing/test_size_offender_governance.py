"""Tests for chronic module-size-offender governance.

Covers:
- ``_is_chronic_offender`` threshold logic (window and growth triggers)
- ``_parse_git_diff_output`` diff parsing
- ``_emit_size_violation_alerts`` operator-alert suppression
- Repeated chronic and first-time violations remain strategist-internal
- Acceptance criterion: ``test_chronic_offender_escalates_not_silently_bumped``
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import select

from app import hotfix_backmerge as hb
from app import size_limit_policy as slp
from app.models import Alert, Project
from app.size_limit_policy import (
    CHRONIC_WINDOW_DAYS,
    BumpHistoryUnavailableError,
    BumpRecord,
    _is_chronic_offender,
    _parse_git_diff_output,
    classify_breach,
    parse_bump_history,
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


@pytest.fixture
def real_git_checkout(tmp_path):
    """Pin the real-checkout precondition for the git-history failure tests.

    Under the deployed-layout fallback, :func:`parse_bump_history` only takes the
    git subprocess path when ``_REPO_ROOT`` is a git checkout. In the CI harness
    ``_REPO_ROOT`` resolves to ``/`` (a non-checkout mount), so a test that must
    exercise the git-failure path would silently become the ledger fallback
    there. Patching ``_REPO_ROOT`` to a temp dir with a ``.git`` directory makes
    the git path deterministic everywhere.
    """
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    with patch("app.size_limit_policy._REPO_ROOT", str(checkout)):
        yield


def _deployed_layout(tmp_path, mocker, ledger_path: str | None = None):
    """Pin the deployed backend layout: no git binary, ``_REPO_ROOT`` not a checkout.

    Optionally repoints the committed ledger mirror to a hermetic fixture file.
    Returns the ledger path in effect.
    """
    mocker.patch("app.size_limit_policy.shutil.which", return_value=None)
    mocker.patch("app.size_limit_policy._REPO_ROOT", str(tmp_path / "deployed"))
    if ledger_path is not None:
        mocker.patch(
            "app.size_limit_policy._CAP_BUMP_HISTORY_LEDGER_PATH", str(ledger_path)
        )
        return str(ledger_path)
    return slp._CAP_BUMP_HISTORY_LEDGER_PATH


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
    with patch("app.hotfix_backmerge.parse_bump_history", return_value=[]):
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

    with patch("app.hotfix_backmerge.parse_bump_history", return_value=chronic_history):
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
    """If parse_bump_history raises, the audience gate already suppressed the alert."""
    project = await _make_project(db_session)

    with patch(
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


def test_parse_bump_history_raises_on_git_failure(real_git_checkout):
    """When git history cannot be read in a real checkout, parse_bump_history
    must raise BumpHistoryUnavailableError rather than returning an empty list —
    so callers can distinguish "no bumps on record" from "history could not be
    read". (The deployed-layout case is covered by the ledger-fallback tests:
    there the same unreadable-git condition is what makes the ledger mirror the
    history source instead.)"""
    with patch(
        "app.size_limit_policy.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        with pytest.raises(BumpHistoryUnavailableError):
            parse_bump_history("raises_git.py", "backend")


def test_parse_bump_history_raises_on_nonzero_git_exit(real_git_checkout):
    """A non-zero git log exit in a real checkout (e.g. a truncated/shallow
    task-VM clone) is also unreadable history and must raise, not degrade to an
    empty list."""

    result = subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr="fatal: bad revision"
    )
    with patch("app.size_limit_policy.subprocess.run", return_value=result):
        with pytest.raises(BumpHistoryUnavailableError):
            parse_bump_history("raises_rc.py", "backend")


def test_classify_breach_git_read_failure_refuses_bump(real_git_checkout):
    """When bump history is unreadable in a real checkout, classify_breach must
    FAIL CLOSED — refuse the bump (no suggested_new_limit) and route to
    decomposition rather than emitting a routine suggested_new_limit for a
    possibly chronic file."""
    with patch(
        "app.size_limit_policy.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        verdict = classify_breach("classify_git.py", 1720)

    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is None
    assert verdict["history_unavailable"] is True
    assert "decomposition" in verdict["reason"].lower()


def test_classify_breach_unreadable_history_refuses_bump():
    """Regression (direction success evidence): an injected failure in the
    history read must route the chronic-offender gate to decomposition with no
    suggested_new_limit — the guard distinguishes "history shows no bumps" from
    "history could not be read" and refuses in the second case."""
    with patch(
        "app.size_limit_policy.parse_bump_history",
        side_effect=BumpHistoryUnavailableError("git log failed for x"),
    ):
        verdict = classify_breach("unreadable.py", 1720)

    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is None
    assert verdict["history_unavailable"] is True
    assert "decomposition" in verdict["reason"].lower()


def test_classify_breach_unreadable_history_override_still_grants_bump():
    """An explicit operator override (override_chronic=True) still grants a bump
    even when history is unreadable — matching the documented override path for a
    verified chronic offender and keeping consumers' ``assert new_cap is not None``
    invariant intact."""
    with patch(
        "app.size_limit_policy.parse_bump_history",
        side_effect=BumpHistoryUnavailableError("git log failed for x"),
    ):
        verdict = classify_breach("override_unreadable.py", 1720, override_chronic=True)

    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is not None
    assert verdict["suggested_new_limit"] > 1720
    assert verdict["history_unavailable"] is True


# ---------------------------------------------------------------------------
# Deployed-layout ledger fallback — the production ``app`` stage ships no git
# binary and ``_REPO_ROOT`` resolves to ``/`` (a non-checkout), so the history-
# readable branch is only reachable through the committed cap-bump-history
# ledger. These pin that branch reachable and the guard not weakened.
# ---------------------------------------------------------------------------


def test_classify_breach_reaches_history_readable_branch_in_deployed_layout(
    tmp_path, mocker
):
    """Acceptance criterion: in the deployed backend layout — no git binary and
    ``_REPO_ROOT`` is not a checkout — the chronic-offender guard must REACH its
    history-readable branch by reading the committed cap-bump-history ledger,
    instead of failing closed on history it can never obtain.

    Regression direction: if the ledger fallback is removed or the shipped
    ledger stops being readable, ``classify_breach`` returns
    ``history_unavailable=True`` under this exact layout and the test fails —
    the deployed layout is precisely where the guard previously could only ever
    reach its fail-closed branch.
    """
    slp._bump_history_cache.clear()
    ledger_path = _deployed_layout(tmp_path, mocker)

    # The ledger mirror ships in the image; the real committed ledger must be
    # readable at its shipped path under the deployed layout.
    assert os.path.isfile(ledger_path)

    # A real chronic offender recorded in the ledger (4 bumps — fires the growth
    # trigger regardless of the 180-day window aging).
    history = parse_bump_history("test_release_qualifier.py", "testing")
    assert len(history) >= 3

    verdict = classify_breach("test_release_qualifier.py", 2400, section="testing")

    assert verdict["history_unavailable"] is False  # readable branch REACHED
    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is None
    assert verdict["bump_count"] == len(history)


def test_deployed_layout_routine_breach_still_classified(tmp_path, mocker):
    """A genuine first-time breach in the deployed layout (no history on record)
    must still classify as routine and receive a suggested new limit — the
    ledger fallback does not turn every breach into a chronic refusal."""
    slp._bump_history_cache.clear()
    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps({"schema_version": 1, "history": {}}), encoding="utf-8"
    )
    _deployed_layout(tmp_path, mocker, ledger_path=ledger_path)

    verdict = classify_breach("brand_new_module.py", 1720)

    assert verdict["history_unavailable"] is False
    assert verdict["is_chronic"] is False
    assert verdict["suggested_new_limit"] is not None
    assert verdict["suggested_new_limit"] > 1720
    assert verdict["bump_count"] == 0


def test_deployed_layout_missing_ledger_fails_closed(tmp_path, mocker):
    """A deployed layout whose ledger mirror is missing/corrupt must STILL fail
    closed — an unreadable mirror can never silently degrade to 'no bump
    history' and grant a routine bump to a possibly chronic offender."""
    slp._bump_history_cache.clear()
    _deployed_layout(tmp_path, mocker, ledger_path=str(tmp_path / "missing.json"))

    verdict = classify_breach("ghost_module.py", 1720)

    assert verdict["history_unavailable"] is True
    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is None


def test_ledger_with_pending_bumps_appends_and_dedups():
    """ledger_with_pending_bumps appends a new bump and does not duplicate one
    already recorded — a retried rollup cycle (or a regenerated --fix) is
    idempotent."""
    ledger = {
        "schema_version": 1,
        "history": {
            "backend": {
                "m.py": [
                    {
                        "commit_sha": "s1",
                        "commit_date": "2026-01-01T00:00:00+00:00",
                        "old_cap": 100,
                        "new_cap": 200,
                    }
                ]
            }
        },
    }
    updated = slp.ledger_with_pending_bumps(ledger, [("backend", "m.py", 100, 200)])
    assert len(updated["history"]["backend"]["m.py"]) == 1  # dedup, no duplicate

    updated = slp.ledger_with_pending_bumps(ledger, [("backend", "m.py", 200, 300)])
    assert len(updated["history"]["backend"]["m.py"]) == 2
    assert updated["history"]["backend"]["m.py"][-1]["old_cap"] == 200
    assert updated["history"]["backend"]["m.py"][-1]["new_cap"] == 300


def _real_repo_checkout() -> str | None:
    """Return a path to a real git checkout of this repo, or None."""
    candidates = [slp._REPO_ROOT]
    if os.path.isdir("/repo/.git"):
        candidates.append("/repo")
    for root in candidates:
        if os.path.isdir(os.path.join(root, ".git")):
            return root
    return None


def test_cap_bump_history_ledger_matches_git():
    """The committed cap-bump-history ledger must mirror the git-recorded bumps.

    The deployed backend reads the ledger (not git), so a ledger that drifts
    from git history would change chronic-offender classification between dev
    and production. Every bump the readable git history shows must be present in
    the ledger (git ⊆ ledger). In a full (non-shallow) checkout the reverse is
    checked too — the two must agree exactly. This is the guard that keeps
    ``check_size_limits.py --fix`` and the rollup preflight honest about
    updating the mirror.
    """
    repo_root = _real_repo_checkout()
    if repo_root is None:
        pytest.skip("no git checkout of this repo available for the ledger guard")
    git_out = subprocess.run(
        [
            "git",
            "log",
            "--all",
            "--format=COMMIT %H %aI",
            "-p",
            "--",
            slp._MODULE_SIZE_LIMITS_RELPATH,
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=30,
        check=True,
    ).stdout

    with open(slp._CAP_BUMP_HISTORY_LEDGER_PATH, encoding="utf-8") as f:
        ledger = json.load(f)
    ledger_history = ledger.get("history", {})
    with open(slp._MODULE_SIZE_LIMITS_PATH, encoding="utf-8") as f:
        caps = json.load(f)

    mismatches = []
    for section, keys in caps.items():
        if not isinstance(keys, dict):
            continue
        for key in keys:
            git_pairs = sorted(
                (r["old_cap"], r["new_cap"])
                for r in _parse_git_diff_output(git_out, key)
            )
            ledger_pairs = sorted(
                (r["old_cap"], r["new_cap"])
                for r in ledger_history.get(section, {}).get(key, [])
            )
            if not all(p in ledger_pairs for p in git_pairs):
                mismatches.append((section, key, git_pairs, ledger_pairs))

    assert not mismatches, (
        "cap-bump-history ledger is missing git-recorded bumps "
        "(regenerate with check_size_limits.py --fix): " + repr(mismatches)
    )

    # In a full-history checkout the mirror must also not contain phantom
    # records — the ledger and git must agree exactly.
    if not os.path.exists(os.path.join(repo_root, ".git", "shallow")):
        for section, keys in caps.items():
            if not isinstance(keys, dict):
                continue
            for key in keys:
                git_pairs = sorted(
                    (r["old_cap"], r["new_cap"])
                    for r in _parse_git_diff_output(git_out, key)
                )
                ledger_pairs = sorted(
                    (r["old_cap"], r["new_cap"])
                    for r in ledger_history.get(section, {}).get(key, [])
                )
                assert git_pairs == ledger_pairs, (
                    f"ledger drifted from git for {section}/{key}"
                )
