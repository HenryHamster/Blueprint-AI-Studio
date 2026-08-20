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
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import select

from app import hotfix_backmerge as hb
from app import release_qualifier as rq
from app import size_limit_policy as slp
from app.models import Alert, Project
from app.size_limit_policy import (
    CHRONIC_BUMP_THRESHOLD,
    CHRONIC_WINDOW_DAYS,
    BumpHistoryUnavailableError,
    BumpRecord,
    UNATTRIBUTED_COMMIT_SHA,
    _is_chronic_offender,
    _parse_git_diff_output,
    cap_history_chain_breaks,
    classify_breach,
    parse_bump_history,
)
from testing import check_size_limits as csl


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


def _write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line {i}" for i in range(count)) + "\n")


def _checker_repo(root: Path, *, source_exists: bool = True) -> Path:
    """Create the minimal checker checkout used by baseline attribution tests."""
    testing_dir = root / "backend" / "testing"
    testing_dir.mkdir(parents=True)
    (root / "backend" / "app").mkdir(parents=True)
    (testing_dir / "module_size_limits.json").write_text(
        json.dumps(
            {"_doc": "fixture", "backend": {"deleted.py": 100}},
            indent=2,
        )
        + "\n"
    )
    if source_exists:
        _write_lines(root / "backend" / "app" / "deleted.py", 20)
    return root


def _ledger_finding(
    *,
    status: str,
    sha: str = "deadbeef" * 5,
) -> tuple:
    if status == "phantom":
        return (
            "backend",
            "deleted.py",
            50,
            100,
            sha,
            "commit's diff shows no recorded bump for this key/pair",
        )
    return (
        "backend",
        "deleted.py",
        50,
        100,
        sha,
        "2026-01-01T00:00:00+00:00",
        status,
    )


def _run_checker(repo: Path, base: Path) -> int:
    return csl.main(
        [
            "--json",
            str(repo / "backend" / "testing" / "module_size_limits.json"),
            "--repo-root",
            str(repo),
            "--base-root",
            str(base),
            "--sections",
            "backend",
        ]
    )


def test_checker_inherited_stale_cap_does_not_fail_unrelated_diff(tmp_path, capsys):
    """A stale cap already present on base is advisory, not task-owned drift."""
    repo = _checker_repo(tmp_path / "repo", source_exists=False)
    base = _checker_repo(tmp_path / "base", source_exists=False)

    assert _run_checker(repo, base) == 0
    output = capsys.readouterr().out
    assert "[pre-existing] backend/deleted.py" in output
    assert "[stale-cap]" not in output


def test_checker_stale_cap_introduced_by_diff_still_fails(tmp_path, capsys):
    """Deleting a file on the task branch keeps the stale-cap finding blocking."""
    repo = _checker_repo(tmp_path / "repo", source_exists=False)
    base = _checker_repo(tmp_path / "base", source_exists=True)

    assert _run_checker(repo, base) == 1
    output = capsys.readouterr().out
    assert "[stale-cap] backend/deleted.py" in output
    assert "[pre-existing]" not in output


@pytest.mark.parametrize("finding_kind", ["phantom", "absent"])
def test_checker_inherited_ledger_finding_does_not_fail_unrelated_diff(
    tmp_path, capsys, mocker, finding_kind
):
    """The same phantom or absent record on base is not blamed on this task."""
    repo = _checker_repo(tmp_path / "repo")
    base = _checker_repo(tmp_path / "base")
    finding = _ledger_finding(
        status=(
            slp.LEDGER_BUMP_ABSENT
            if finding_kind == "absent"
            else "phantom"
        )
    )

    def findings_for(*, repo_root, **_kwargs):
        return [finding]

    mocker.patch(
        "app.size_limit_policy.phantom_ledger_records",
        side_effect=(findings_for if finding_kind == "phantom" else lambda **_kwargs: []),
    )
    mocker.patch(
        "app.size_limit_policy.missing_ledger_bump_records",
        side_effect=(findings_for if finding_kind == "absent" else lambda **_kwargs: []),
    )
    mocker.patch("app.size_limit_policy._mainline_git_ref", return_value="main")
    (repo / ".git").mkdir()
    (base / ".git").mkdir()

    assert _run_checker(repo, base) == 0
    output = capsys.readouterr().out
    marker = "[ledger-phantom]" if finding_kind == "phantom" else "[ledger-missing]"
    assert "[pre-existing] backend/deleted.py" in output
    assert marker not in output


@pytest.mark.parametrize("finding_kind", ["phantom", "absent"])
def test_checker_new_ledger_finding_still_fails(tmp_path, capsys, mocker, finding_kind):
    """A ledger finding absent from base remains attributed to this task."""
    repo = _checker_repo(tmp_path / "repo")
    base = _checker_repo(tmp_path / "base")
    finding = _ledger_finding(
        status=(
            slp.LEDGER_BUMP_ABSENT
            if finding_kind == "absent"
            else "phantom"
        )
    )

    def findings_for(*, repo_root, **_kwargs):
        return [finding] if Path(repo_root) == repo else []

    mocker.patch(
        "app.size_limit_policy.phantom_ledger_records",
        side_effect=(findings_for if finding_kind == "phantom" else lambda **_kwargs: []),
    )
    mocker.patch(
        "app.size_limit_policy.missing_ledger_bump_records",
        side_effect=(findings_for if finding_kind == "absent" else lambda **_kwargs: []),
    )
    mocker.patch("app.size_limit_policy._mainline_git_ref", return_value="main")
    (repo / ".git").mkdir()
    (base / ".git").mkdir()

    assert _run_checker(repo, base) == 1
    output = capsys.readouterr().out
    marker = "[ledger-phantom]" if finding_kind == "phantom" else "[ledger-missing]"
    assert marker in output
    assert "[pre-existing]" not in output


def test_self_heal_clamps_governed_release_qualifier_cap(tmp_path, mocker):
    """The real fix path honors release_qualifier.py's declared cap relationship."""
    source_repo_root = next(
        candidate
        for candidate in (Path(__file__).resolve().parents[2], Path("/repo"))
        if (candidate / "backend" / "app" / "release_qualifier.py").is_file()
    )
    repo_root = tmp_path / "repo"
    qualifier_path = repo_root / "backend" / "app" / "release_qualifier.py"
    qualifier_path.parent.mkdir(parents=True)
    qualifier_path.write_text(
        (source_repo_root / "backend" / "app" / "release_qualifier.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    limits_path = tmp_path / "module_size_limits.json"
    limits_path.write_text(
        json.dumps(
            {
                "_doc": "fixture",
                "backend": {"release_qualifier.py": 10300},
            }
        )
        + "\n"
    )

    # A generic 15% bump for the current file would exceed 12,000 lines. The
    # production fixer must discover and clamp to the module's measured 10900
    # line governance constant.
    mocker.patch("app.size_limit_policy.parse_bump_history", return_value=[])
    assert (
        csl.main(
            [
                "--fix",
                "--sections",
                "backend",
                "--repo-root",
                str(repo_root),
                "--json",
                str(limits_path),
            ]
        )
        == 0
    )

    limits = json.loads(limits_path.read_text())
    cap = limits["backend"]["release_qualifier.py"]
    count = len(qualifier_path.read_text(encoding="utf-8").splitlines())
    assert cap == 10900
    assert cap == rq._MODULE_SIZE_GOVERNANCE_CAP
    assert count <= cap
    assert cap - count < cap * 0.10


def test_self_heal_removes_cap_for_deleted_file(tmp_path):
    """--fix removes stale entries once a capped source file is deleted."""
    repo_root = tmp_path / "repo"
    source_root = repo_root / "backend" / "app"
    _write_lines(source_root / "kept.py", 20)
    limits_path = repo_root / "backend" / "testing" / "module_size_limits.json"
    limits_path.parent.mkdir(parents=True)
    limits_path.write_text(
        json.dumps(
            {
                "_doc": "fixture",
                "backend": {"deleted.py": 100, "kept.py": 100},
            }
        )
        + "\n"
    )

    assert (
        csl.main(
            [
                "--fix",
                "--sections",
                "backend",
                "--repo-root",
                str(repo_root),
                "--json",
                str(limits_path),
            ]
        )
        == 0
    )

    limits = json.loads(limits_path.read_text())
    assert "deleted.py" not in limits["backend"]
    assert limits["backend"]["kept.py"] == 100


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


def test_cap_history_chain_breaks_only_report_upward_gaps():
    """A missing cap raise is actionable; a cap decrease is not a ledger error."""
    history = [
        _record(1000, 1050, 300),
        _record(1100, 1150, 200),  # missing 1050 -> 1100 raise
        _record(1150, 1200, 100),
        _record(1150, 1180, 50),  # legitimate decrease in the next old cap
    ]

    assert cap_history_chain_breaks(history) == [(1050, 1100)]


def test_chronic_history_gap_fails_closed_before_growth_rebaseline():
    """An upward gap remains chronic even when the damaged suffix grew <30%."""
    history = [
        _record(1000, 1050, 300),
        _record(1100, 1150, 200),
        _record(1150, 1200, 100),
    ]

    # Without the missing 1050 -> 1100 record, the old growth calculation sees
    # only 20% growth and would silently loosen the chronic-offender gate.
    assert _is_chronic_offender(history)


# ---------------------------------------------------------------------------
# is_breach — the guardrail's fundamental predicate (single source of truth)
# ---------------------------------------------------------------------------


def test_is_breach_true_when_count_exceeds_cap():
    assert slp.is_breach(1200, 1180)
    assert slp.is_breach(1193, 1180)


def test_is_breach_false_when_within_or_at_cap():
    assert not slp.is_breach(1180, 1180)
    assert not slp.is_breach(1000, 1180)


def test_is_breach_missing_file_never_breaches():
    """A file absent at the ref (count is None) cannot have grown past its cap."""
    assert not slp.is_breach(None, 1180)


def test_is_breach_missing_cap_never_breaches():
    """An uncapped file (cap is None) has no limit to breach."""
    assert not slp.is_breach(1200, None)


def test_is_breach_missing_both_never_breaches():
    assert not slp.is_breach(None, None)


def test_classify_size_record_delegates_breach_to_policy():
    """check_size_limits.classify_size_record must use the production predicate.

    Guards the single-source-of-truth contract: the standalone checker and the
    CI-compliance evaluator must classify a breach via
    ``app.size_limit_policy.is_breach`` rather than re-deriving the comparison
    inline, so a drift between the two is impossible.
    """
    from testing.check_size_limits import classify_size_record

    assert classify_size_record("m.py", 1200, 1180, "backend", with_verdict=False)["breach"]
    assert not classify_size_record(
        "m.py", 1180, 1180, "backend", with_verdict=False
    )["breach"]
    assert not classify_size_record(
        "m.py", None, 1180, "backend", with_verdict=False
    )["breach"]


def test_fix_respects_declared_governing_cap(tmp_path, mocker):
    """``--fix`` keeps a governed source constant, JSON cap, and ledger aligned.

    This is intentionally an end-to-end checker test: it exercises the shared
    routine-breach classifier, the local repair writer, and the pending ledger
    record rather than merely asserting the clamp helper in isolation. The
    repaired cap CLAMPS to the module's declared constant instead of taking
    generic headroom, and the declared constant itself is never raised — a
    governed module that outgrows its constant must be decomposed, not bumped.
    """
    repo = tmp_path / "repo"
    source_path = repo / "backend" / "app" / "release_qualifier.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "_MODULE_SIZE_GOVERNANCE_CAP: int = 10500\n"
        + "pass\n" * 10400,
        encoding="utf-8",
    )
    testing_dir = repo / "backend" / "testing"
    testing_dir.mkdir()
    limits_path = testing_dir / "module_size_limits.json"
    limits_path.write_text(
        json.dumps(
            {"_doc": "test", "backend": {"release_qualifier.py": 10300}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Hermetic fixture: no mainline checkout means only this repair's pending
    # evidence should be present in the regenerated mirror.
    mocker.patch("app.size_limit_policy._git_reads_history_here", return_value=False)
    mocker.patch("app.size_limit_policy.parse_bump_history", return_value=[])

    assert csl.main(
        [
            "--json",
            str(limits_path),
            "--repo-root",
            str(repo),
            "--sections",
            "backend",
            "--fix",
        ]
    ) == 0

    repaired = json.loads(limits_path.read_text(encoding="utf-8"))
    # A generic +15% bump for a 10,401-line file would be 11,950; the governed
    # module clamps to its declared 10,500 constant instead.
    assert repaired["backend"]["release_qualifier.py"] == 10500
    assert "_MODULE_SIZE_GOVERNANCE_CAP: int = 10500" in source_path.read_text(
        encoding="utf-8"
    )
    ledger = json.loads(
        (testing_dir / "cap_bump_history.json").read_text(encoding="utf-8")
    )
    record = ledger["history"]["backend"]["release_qualifier.py"][-1]
    assert record["commit_sha"] == UNATTRIBUTED_COMMIT_SHA
    assert (record["old_cap"], record["new_cap"]) == (10300, 10500)
    assert record["commit_date"]


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
    ), pytest.raises(BumpHistoryUnavailableError):
        parse_bump_history("raises_git.py", "backend")


def test_parse_bump_history_raises_on_nonzero_git_exit(real_git_checkout):
    """A non-zero git log exit in a real checkout (e.g. a truncated/shallow
    task-VM clone) is also unreadable history and must raise, not degrade to an
    empty list."""

    result = subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr="fatal: bad revision"
    )
    with patch("app.size_limit_policy.subprocess.run", return_value=result), pytest.raises(
        BumpHistoryUnavailableError
    ):
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

    # A real chronic capped testing file recorded in the ledger.  Do not use a
    # deleted file here: stale cap/ledger records are now intentionally pruned
    # by --fix, and this test's purpose is the deployed-layout fallback rather
    # than preserving obsolete history.
    history = parse_bump_history("test_acceptance_criteria.py", "testing")
    assert len(history) >= 3

    verdict = classify_breach("test_acceptance_criteria.py", 16000, section="testing")

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
    assert updated["history"]["backend"]["m.py"][-1]["commit_sha"] == (
        UNATTRIBUTED_COMMIT_SHA
    )
    assert all(record["commit_sha"] for record in updated["history"]["backend"]["m.py"])


def test_ledger_with_pending_bumps_preserves_observed_provenance():
    """Observed git evidence upgrades a matching unattributed placeholder."""
    ledger = {
        "history": {
            "backend": {
                "m.py": [
                    {
                        "commit_sha": UNATTRIBUTED_COMMIT_SHA,
                        "commit_date": "2026-01-01T00:00:00+00:00",
                        "old_cap": 100,
                        "new_cap": 200,
                    }
                ]
            }
        }
    }

    updated = slp.ledger_with_pending_bumps(
        ledger,
        [
            (
                "backend",
                "m.py",
                100,
                200,
                "realcommitsha",
                "2026-02-03T04:05:06+00:00",
            )
        ],
    )

    record = updated["history"]["backend"]["m.py"][0]
    assert record["commit_sha"] == "realcommitsha"
    assert record["commit_date"] == "2026-02-03T04:05:06+00:00"


def test_restored_cap_bumps_keep_strategic_resident_session_chronic():
    """The three restored b644fbb02 records keep governance above threshold.

    Deleting the earliest record lowers ``_is_chronic_offender``'s growth
    baseline and can drop a file below the three-record chronic threshold. This
    assertion pins the repair's bias direction: restoration cannot loosen the
    governance verdict.
    """
    with open(slp._CAP_BUMP_HISTORY_LEDGER_PATH, encoding="utf-8") as f:
        ledger = json.load(f)

    resident = ledger["history"]["backend"]["strategic_resident_session.py"]
    resident_test = ledger["history"]["testing"]["test_strategic_resident_session.py"]
    shard_test = ledger["history"]["testing"]["test_shard_balance.py"]

    assert {
        (record["commit_sha"], record["old_cap"], record["new_cap"])
        for record in resident + resident_test + shard_test
    } >= {
        ("b644fbb02", 4200, 4900),
        ("b644fbb02", 2050, 2500),
        ("b644fbb02", 100, 350),
    }
    assert len(resident) >= CHRONIC_BUMP_THRESHOLD
    assert len(resident_test) >= CHRONIC_BUMP_THRESHOLD
    assert _is_chronic_offender(resident)
    assert _is_chronic_offender(resident_test)


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
    and production. Every bump the readable MAINLINE git history shows at-or-
    after the ledger's coverage window must be present in the ledger (git ⊆
    ledger); AND no ledger record may be a phantom the mainline history cannot
    justify (ledger ⊆ git) — checked via ``slp.phantom_ledger_records``. The
    scope is the mainline integration branch's ancestry
    (``slp._mainline_git_ref``), never ``--all``: bumps on abandoned
    task/research branches never merged and must not enter the mirror.  This is
    the guard that keeps ``check_size_limits.py --fix`` and the rollup preflight
    honest about updating the mirror.

    The ledger deliberately does NOT mirror pre-ratchet bump history: the
    2026-05-05 guardrail ratchet reset the baselines, and bumps below the
    ratchet caps would wrongly extend the chronic-offender window.  The
    forward direction therefore scopes to the ledger's own coverage window (its
    earliest record, per-key with a global fallback), so pre-window git bumps
    are legitimately absent.

    A shallow/truncated checkout (CI's depth-1 clone, an agent task VM) does NOT
    skip this guard: the git ⊆ ledger direction is still evaluable — every bump
    the partial mainline history DOES show must be present in the ledger — and
    ``phantom_ledger_records`` is deliberately NOT skipped on a truncated
    checkout either. It verifies every record the partial history CAN
    adjudicate (a resolvable SHA whose diff shows no bump, or a commit proven
    outside the mainline ancestry) and skips only records the partial history
    cannot resolve. The reverse direction is therefore exercised on every CI
    shard and agent VM — exactly where the ledger most often drifts — instead
    of being skipped there.
    """
    repo_root = _real_repo_checkout()
    if repo_root is None:
        pytest.skip("no git checkout of this repo available for the ledger guard")
    # The CI backend shards bind the checkout read-only at /repo into a root
    # container while the runner user owns the files, so git inside the
    # container refuses every command with "detected dubious ownership" (exit
    # 128) unless the path is marked safe.  ``-c safe.directory=<repo_root>``
    # is the same injection runner_isolation.py performs via GIT_CONFIG_* for
    # cloned workspaces — without it this guard dies before reading history.
    git_out = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo_root}",
            "log",
            slp._mainline_git_ref(repo_root),
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

    # The ledger deliberately does NOT mirror pre-ratchet bump history: the
    # 2026-05-05 guardrail ratchet reset the baselines, and bumps below the
    # ratchet caps would wrongly extend the chronic-offender window.  The
    # ledger's coverage therefore starts at its earliest record; a git bump
    # that predates the ledger's window for its key is legitimately absent.
    # For a key that holds ledger records the window is that key's earliest
    # record; for a key with none (a cap established before the window that has
    # not moved since) the window falls back to the ledger's global earliest
    # record.  Only bumps at-or-after the window must be present (git ⊆
    # ledger); the phantom check below is the reverse direction.
    global_start = min(
        (
            slp._parse_iso(r["commit_date"])
            for _section, _keys in ledger_history.items()
            for _key, _recs in _keys.items()
            for r in _recs
            if r.get("commit_date")
        ),
        default=None,
    )
    mismatches = []
    for section, keys in caps.items():
        if not isinstance(keys, dict):
            continue
        for key in keys:
            git_records = _parse_git_diff_output(git_out, key)
            ledger_recs = ledger_history.get(section, {}).get(key, [])
            ledger_pairs = {
                (r["old_cap"], r["new_cap"]) for r in ledger_recs
            }
            window_start = min(
                (
                    slp._parse_iso(r["commit_date"])
                    for r in ledger_recs
                    if r.get("commit_date")
                ),
                default=global_start,
            )
            for rec in git_records:
                pair = (rec["old_cap"], rec["new_cap"])
                if pair in ledger_pairs:
                    continue
                if window_start is not None and slp._parse_iso(
                    rec["commit_date"]
                ) < window_start:
                    # Pre-ledger (pre-ratchet) bump — legitimately absent.
                    continue
                mismatches.append(
                    (
                        section,
                        key,
                        (rec["old_cap"], rec["new_cap"]),
                        rec["commit_sha"][:12],
                        rec["commit_date"],
                        sorted(ledger_pairs),
                    )
                )

    assert not mismatches, (
        "cap-bump-history ledger is missing git-recorded bumps "
        "(regenerate with check_size_limits.py --fix): " + repr(mismatches)
    )

    # Reverse direction (ledger ⊆ git): no ledger record may be a phantom the
    # mainline history cannot justify — a wrong old_cap, a decrease mis-filed as
    # a bump, a commit outside the mainline ancestry, or a non-resolving SHA.
    # Unlike the git ⊆ ledger direction this is not limited to current-caps keys
    # (a phantom on a removed key still proves the ledger was fabricated), and
    # unlike the old exact-equality reverse check it is NOT skipped on a
    # truncated checkout: phantom_ledger_records verifies every record the
    # partial history CAN adjudicate and skips only records it cannot resolve.
    # This is the half of the ledger guard that used to be silently skipped on
    # every shallow CI shard and agent VM — exactly where drift accumulates.
    phantoms = slp.phantom_ledger_records(repo_root=repo_root)
    assert phantoms == [], (
        "cap-bump-history ledger holds phantom records the mainline git history "
        "cannot justify (regenerate with check_size_limits.py --fix): "
        + repr(phantoms)
    )


# ---------------------------------------------------------------------------
# Phantom-record unit tests — the reverse ledger guard (ledger ⊆ git).  A
# ledger record whose commit_sha does not resolve to a mainline bump of the
# exact (old_cap -> new_cap) pair is a phantom: a wrong old_cap, a decrease
# mis-filed as a bump, a non-ancestor commit, or a fabricated SHA.  These run
# against hermetic repos so the verdict is deterministic in every environment.
# ---------------------------------------------------------------------------


def _hermetic_head_sha(repo: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _phantom_ledger(repo: str, key: str, record: dict) -> list:
    """Run phantom_ledger_records against a hermetic repo + single-record ledger."""
    ledger = {
        "schema_version": 1,
        "history": {"backend": {key: [record]}},
    }
    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        return slp.phantom_ledger_records(repo_root=repo, history=ledger)


def test_phantom_ledger_records_accepts_matching_bump(tmp_path, mocker):
    """A ledger record whose commit actually made the exact bump is NOT a
    phantom — the commit's diff shows the pair, so the mirror is truthful."""
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "bump to 200", date="2026-01-02T00:00:00+00:00")
    bump_sha = _hermetic_head_sha(repo)

    phantoms = _phantom_ledger(
        repo,
        "m.py",
        {
            "commit_sha": bump_sha,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    )
    assert phantoms == []


def test_phantom_ledger_records_flags_wrong_old_cap(tmp_path, mocker):
    """A ledger record citing a real bump commit but with a WRONG old_cap is a
    phantom — the commit's diff proves the raise started from 100, not 50."""
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "bump to 200", date="2026-01-02T00:00:00+00:00")
    bump_sha = _hermetic_head_sha(repo)

    phantoms = _phantom_ledger(
        repo,
        "m.py",
        {
            "commit_sha": bump_sha,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 50,
            "new_cap": 200,
        },
    )
    assert len(phantoms) == 1
    section, key, old_cap, new_cap, _sha, reason = phantoms[0]
    assert (section, key, old_cap, new_cap) == ("backend", "m.py", 50, 200)
    assert "no recorded bump" in reason


def test_phantom_ledger_records_preserves_merge_commit_provenance(tmp_path, mocker):
    """A merge commit does not provide an attributable single-parent cap diff.

    ``git show`` renders a merge as a combined diff (or no file diff at all),
    so the ordinary ``- old/+ new`` parser cannot establish that the ledger
    record is fabricated.  The verifier must preserve the record and wait for
    evidence from the side-parent bump commit instead of deleting it.
    """
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")

    subprocess.run(["git", "checkout", "-q", "-b", "cap-bump"], cwd=repo, check=True)
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "bump to 200", date="2026-01-02T00:00:00+00:00")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "merge", "--no-ff", "-q", "cap-bump", "-m", "merge cap bump"],
        cwd=repo,
        check=True,
    )
    merge_sha = _hermetic_head_sha(repo)

    phantoms = _phantom_ledger(
        repo,
        "m.py",
        {
            "commit_sha": merge_sha,
            "commit_date": "2026-01-03T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    )

    assert phantoms == []


def test_phantom_ledger_records_skips_empty_sha_pending(tmp_path, mocker):
    """An empty commit_sha is a rollup-pending bump written before the commit
    exists — there is nothing to verify, so it is never a phantom."""
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")

    phantoms = _phantom_ledger(
        repo,
        "m.py",
        {
            "commit_sha": "",
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    )
    assert phantoms == []


def test_phantom_ledger_records_preserves_unresolvable_sha_on_full(tmp_path, mocker):
    """An unresolvable SHA is preserved even on a complete checkout.

    A missing object is absent corroboration, not positive proof that the
    ledger record was fabricated. The self-heal must not delete on that basis.
    """
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")

    phantoms = _phantom_ledger(
        repo,
        "m.py",
        {
            "commit_sha": "deadbeef" * 5,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    )
    assert phantoms == []


def test_phantom_ledger_records_skips_beyond_boundary_on_shallow(tmp_path, mocker):
    """On a shallow clone a ledger SHA that does not resolve is SKIPPED, not
    flagged — the object may simply be beyond the fetch boundary, and a genuine
    bump is indistinguishable from a fabricated SHA when the object is gone."""
    checkout = _init_shallow_repo(tmp_path)

    phantoms = _phantom_ledger(
        checkout,
        "m.py",
        {
            "commit_sha": "deadbeef" * 5,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    )
    assert phantoms == []


def test_phantom_ledger_records_preserves_bump_at_shallow_boundary(
    tmp_path, mocker
):
    """A real bump at the shallow boundary is unverifiable, not a phantom.

    Depth-one clones hide the bump commit's parent. Git consequently renders
    ``module_size_limits.json`` as a whole-file addition, which contains no
    parseable ``old -> new`` pair even though the full repository proves the
    ledger record. This is the production task-VM shape.
    """
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=source, check=True
    )
    _hermetic_write_cap(str(source), 100)
    _hermetic_commit(str(source), "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(str(source), 200)
    _hermetic_commit(
        str(source), "bump at boundary", date="2026-01-02T00:00:00+00:00"
    )
    bump_sha = _hermetic_head_sha(str(source))

    checkout = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", f"file://{source}", str(checkout)],
        check=True,
    )

    phantoms = _phantom_ledger(
        str(checkout),
        "m.py",
        {
            "commit_sha": bump_sha,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    )

    assert phantoms == []
    linked = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(linked), "HEAD"],
        cwd=checkout,
        check=True,
    )
    assert (linked / ".git").is_file()
    assert slp._git_history_is_truncated(str(linked)) is True
    assert _phantom_ledger(
        str(linked),
        "m.py",
        {
            "commit_sha": bump_sha,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    ) == []


def test_phantom_ledger_records_preserves_resolvable_bump_beyond_boundary(
    tmp_path, mocker
):
    """A reference clone must not turn a truncated ancestry probe into proof.

    Task VMs clone with a shallow mainline plus a baked object-store reference.
    An older ledger SHA therefore resolves even though Git will not traverse
    the mainline past its newer shallow boundary; ``merge-base --is-ancestor``
    returns 1 in that ambiguous graph. Preserve the row until a complete
    checkout can adjudicate it.
    """
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=source, check=True
    )
    _hermetic_write_cap(str(source), 100)
    _hermetic_commit(str(source), "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(str(source), 200)
    _hermetic_commit(
        str(source), "real bump", date="2026-01-02T00:00:00+00:00"
    )
    bump_sha = _hermetic_head_sha(str(source))
    (source / "module_size_limits.json").write_text(
        json.dumps({"backend": {"m.py": 200, "later.py": 1}}, indent=2),
        encoding="utf-8",
    )
    _hermetic_commit(
        str(source), "newer boundary", date="2026-01-03T00:00:00+00:00"
    )

    checkout = tmp_path / "shallow-reference"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth=1",
            "--reference-if-able",
            str(source),
            f"file://{source}",
            str(checkout),
        ],
        check=True,
    )
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{bump_sha}^{{commit}}"],
        cwd=checkout,
        check=False,
    ).returncode == 0
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", bump_sha, "main"],
        cwd=checkout,
        check=False,
    ).returncode == 1

    phantoms = _phantom_ledger(
        str(checkout),
        "m.py",
        {
            "commit_sha": bump_sha,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 100,
            "new_cap": 200,
        },
    )

    assert phantoms == []
    wrong_pair = _phantom_ledger(
        str(checkout),
        "m.py",
        {
            "commit_sha": bump_sha,
            "commit_date": "2026-01-02T00:00:00+00:00",
            "old_cap": 50,
            "new_cap": 200,
        },
    )
    assert len(wrong_pair) == 1
    assert "diff shows no recorded bump" in wrong_pair[0][-1]


def test_phantom_ledger_records_flags_non_ancestor_commit(tmp_path, mocker):
    """A commit outside the mainline ancestry is a phantom even when its object
    resolves — the bump it made was never merged, so it must not be in the
    ledger the deployed backend reads."""
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "mainline bump", date="2026-01-02T00:00:00+00:00")
    # Abandoned branch bump (200 -> 300) is never merged back to mainline.
    subprocess.run(["git", "checkout", "-q", "-b", "abandoned"], cwd=repo, check=True)
    _hermetic_write_cap(repo, 300)
    _hermetic_commit(repo, "abandoned bump", date="2026-01-03T00:00:00+00:00")
    abandoned_sha = _hermetic_head_sha(repo)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

    phantoms = _phantom_ledger(
        repo,
        "m.py",
        {
            "commit_sha": abandoned_sha,
            "commit_date": "2026-01-03T00:00:00+00:00",
            "old_cap": 200,
            "new_cap": 300,
        },
    )
    assert len(phantoms) == 1
    assert phantoms[0][5] == "commit is not in the mainline ancestry"


def test_rebuild_prunes_proven_phantom_records(tmp_path, mocker):
    """rebuild_cap_bump_history_ledger prunes a ledger record the mainline git
    history cannot justify (a wrong old_cap), so ``--fix`` self-heals the mirror
    instead of the merge safety net preserving the phantom forever."""
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "bump to 200", date="2026-01-02T00:00:00+00:00")
    bump_sha = _hermetic_head_sha(repo)

    ledger_path = tmp_path / "cap_bump_history.json"
    # Existing ledger holds the REAL bump AND a phantom (wrong old_cap) that the
    # merge safety net would otherwise keep forever.
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "history": {
                    "backend": {
                        "m.py": [
                            {
                                "commit_sha": bump_sha,
                                "commit_date": "2026-01-02T00:00:00+00:00",
                                "old_cap": 100,
                                "new_cap": 200,
                            },
                            {
                                "commit_sha": bump_sha,
                                "commit_date": "2026-01-02T00:00:00+00:00",
                                "old_cap": 50,
                                "new_cap": 200,
                            },
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        slp.rebuild_cap_bump_history_ledger(
            caps_json_path=str(Path(repo) / "module_size_limits.json"),
            ledger_path=str(ledger_path),
        )

    records = json.loads(ledger_path.read_text())["history"]["backend"]["m.py"]
    assert [(r["old_cap"], r["new_cap"]) for r in records] == [(100, 200)]


def test_rebuild_preserves_unverifiable_record(tmp_path, mocker):
    """Failure to corroborate a commit is non-destructive during self-heal."""
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")

    ledger_path = tmp_path / "cap_bump_history.json"
    unverifiable = {
        "commit_sha": "deadbeef" * 5,
        "commit_date": "2026-01-02T00:00:00+00:00",
        "old_cap": 100,
        "new_cap": 200,
    }
    ledger_path.write_text(
        json.dumps(
            {"schema_version": 1, "history": {"backend": {"m.py": [unverifiable]}}}
        ),
        encoding="utf-8",
    )

    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        slp.rebuild_cap_bump_history_ledger(
            caps_json_path=str(Path(repo) / "module_size_limits.json"),
            ledger_path=str(ledger_path),
        )

    records = json.loads(ledger_path.read_text())["history"]["backend"]["m.py"]
    assert records == [unverifiable]


# ---------------------------------------------------------------------------
# Ref-scope regression tests — the cap-bump ledger must mirror the MAINLINE
# branch's ancestry, never ``--all`` (abandoned task/research branches' bumps
# never merged and must not enter chronic-offender classification), and a
# shallow/truncated checkout must read the committed ledger mirror (complete)
# rather than the silently-partial ``git log``.
# ---------------------------------------------------------------------------


def _init_hermetic_repo(tmp_path, branch: str = "main") -> str:
    """Create a git repo with a single ``module_size_limits.json`` at the root.

    Returns the repo path.  Callers write cap values and commit with the
    ``_hermetic_commit`` helper.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    return str(repo)


def _hermetic_write_cap(repo: str, value: int) -> None:
    # Pretty-print exactly like the shipped module_size_limits.json so git diff
    # hunks carry each key on its own line, which _parse_git_diff_output needs.
    (Path(repo) / "module_size_limits.json").write_text(
        json.dumps({"backend": {"m.py": value}}, indent=2), encoding="utf-8"
    )


def _hermetic_commit(repo: str, msg: str, date: str | None = None) -> None:
    subprocess.run(["git", "add", "module_size_limits.json"], cwd=repo, check=True)
    env = dict(os.environ)
    if date is not None:
        # Pin the author date so _parse_git_diff_output's ascending timestamp
        # sort is deterministic even when two commits land in the same second.
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "commit", "-q", "-m", msg], cwd=repo, env=env, check=True
    )


def _init_shallow_repo(tmp_path) -> str:
    """Create a REAL depth-1 shallow clone whose tip is a shallow boundary.

    ``git clone --depth=1`` writes the fetched tip into ``.git/shallow``, so
    ``git log`` would silently return a partial history for that clone.  This
    is the faithful way to exercise the truncated-history paths (a fake
    ``.git/shallow`` entry is not in any ref's ancestry, so the
    mainline-ancestry truncation check would not fire on it).
    """
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=src, check=True)
    _hermetic_write_cap(str(src), 100)
    _hermetic_commit(str(src), "init", date="2026-01-01T00:00:00+00:00")
    clone = tmp_path / "shallow"
    # file:// protocol is required: git ignores --depth for plain local-path
    # clones, which would silently produce a full clone instead of a shallow one.
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", f"file://{src}", str(clone)],
        check=True,
    )
    return str(clone)


def test_mainline_ref_scope_excludes_non_ancestor_bumps(tmp_path, mocker):
    """The ledger's ref scope is the mainline branch's ancestry, never --all.

    Regression for the permanently-red guard: every git read used ``git log
    --all``, which pulls in cap bumps from abandoned task/research branches
    that NEVER merged into the mainline — the 24 modules' "missing" bumps in
    the diagnosis were all non-ancestor commits.  A bump that exists only on an
    abandoned branch must not enter the history the ledger mirrors.
    """
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "mainline bump", date="2026-01-02T00:00:00+00:00")
    # Abandoned branch bump (200 -> 300) is never merged back to mainline.
    subprocess.run(["git", "checkout", "-q", "-b", "abandoned"], cwd=repo, check=True)
    _hermetic_write_cap(repo, 300)
    _hermetic_commit(repo, "abandoned bump", date="2026-01-03T00:00:00+00:00")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

    slp._bump_history_cache.clear()
    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        assert slp._mainline_git_ref() == "main"
        records = slp._read_git_bump_history("m.py")
        assert records is not None
        assert [(r["old_cap"], r["new_cap"]) for r in records] == [(100, 200)]

        # Prove the test would have caught the old --all behavior: the abandoned
        # branch bump is visible under --all but must stay out of the
        # mainline-scoped history.
        all_out = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo}",
                "log",
                "--all",
                "--format=COMMIT %H %aI",
                "-p",
                "--",
                "module_size_limits.json",
            ],
            capture_output=True,
            text=True,
            cwd=repo,
            check=True,
        ).stdout
        all_records = slp._parse_git_diff_output(all_out, "m.py")
        assert [(r["old_cap"], r["new_cap"]) for r in all_records] == [
            (100, 200),
            (200, 300),
        ]


def test_rebuild_ledger_uses_mainline_scope(tmp_path, mocker):
    """The regenerator builds the ledger from mainline ancestry only.

    The ``--fix`` regenerator and the production read must share the same
    scope, or the ledger and its checker drift apart.  A bump on an abandoned
    branch must never enter the regenerated mirror.
    """
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "mainline bump", date="2026-01-02T00:00:00+00:00")
    subprocess.run(["git", "checkout", "-q", "-b", "abandoned"], cwd=repo, check=True)
    _hermetic_write_cap(repo, 300)
    _hermetic_commit(repo, "abandoned bump", date="2026-01-03T00:00:00+00:00")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

    ledger_path = tmp_path / "cap_bump_history.json"
    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        ledger = slp.rebuild_cap_bump_history_ledger(
            caps_json_path=str(Path(repo) / "module_size_limits.json"),
            ledger_path=str(ledger_path),
        )

    records = ledger["history"]["backend"]["m.py"]
    assert [(r["old_cap"], r["new_cap"]) for r in records] == [(100, 200)]


def test_rebuild_ledger_preserves_existing_on_truncated_git(tmp_path, mocker):
    """The regenerator must not replace the mirror with a truncated git view.

    On a shallow checkout (a real depth-1 clone), git is not even run and the
    existing ledger is preserved — a truncated ``git log`` must never truncate
    the mirror (fail-closed, never fail-open).
    """
    repo = _init_shallow_repo(tmp_path)

    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )
    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        ledger = slp.rebuild_cap_bump_history_ledger(
            caps_json_path=str(Path(repo) / "module_size_limits.json"),
            ledger_path=str(ledger_path),
        )

    # Existing record preserved unchanged (git never ran on the truncated checkout).
    records = ledger["history"]["backend"]["m.py"]
    assert [(r["old_cap"], r["new_cap"]) for r in records] == [(100, 200)]


def test_parse_bump_history_reads_ledger_on_shallow_checkout(tmp_path, mocker):
    """A shallow/truncated checkout reads the committed ledger mirror.

    Regression for the shallow-clone fail-closed bug (introduced by pinning the
    ledger ref scope to mainline ancestry): a depth-limited clone — every agent
    task VM, CI's depth-1 checkout — used to raise BumpHistoryUnavailableError
    in ``parse_bump_history`` because its ``git log`` is silently partial, even
    though the committed cap-bump-history ledger is complete and sitting right
    there.  The ledger mirror must be consulted BEFORE failing closed, so a
    shallow checkout classifies from the same complete history the deployed
    layout reads instead of refusing every cap bump.
    """
    checkout = _init_shallow_repo(tmp_path)
    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "history": {
                    "backend": {
                        "shallow_git.py": [
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
        ),
        encoding="utf-8",
    )
    mocker.patch("app.size_limit_policy._REPO_ROOT", checkout)
    mocker.patch(
        "app.size_limit_policy._CAP_BUMP_HISTORY_LEDGER_PATH", str(ledger_path)
    )
    slp._bump_history_cache.clear()
    history = parse_bump_history("shallow_git.py", "backend")
    assert [(r["old_cap"], r["new_cap"]) for r in history] == [(100, 200)]


def test_classify_breach_reads_ledger_on_shallow_checkout(tmp_path, mocker):
    """classify_breach reaches its history-readable branch on a shallow clone.

    The chronic-offender verdict must classify from the committed ledger mirror
    on a depth-limited clone (``history_unavailable=False``) rather than failing
    closed on every module — the cap-bump remediation path is dead for EVERY
    module on a shallow clone if the ledger fallback is not reached.
    """
    checkout = _init_shallow_repo(tmp_path)
    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "history": {
                    "backend": {
                        "m.py": [
                            _record(100, 200, days_ago=30),
                            _record(200, 300, days_ago=20),
                            _record(300, 400, days_ago=10),
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    mocker.patch("app.size_limit_policy._REPO_ROOT", checkout)
    mocker.patch(
        "app.size_limit_policy._CAP_BUMP_HISTORY_LEDGER_PATH", str(ledger_path)
    )
    slp._bump_history_cache.clear()
    verdict = classify_breach("m.py", 450)
    assert verdict["history_unavailable"] is False
    assert verdict["bump_count"] == 3
    assert verdict["is_chronic"] is True
    assert verdict["suggested_new_limit"] is None


def test_mainline_git_ref_falls_back_to_head(tmp_path, mocker):
    """With no mainline branch present, the ref helper falls back to HEAD —
    still a single branch's ancestry, never ``--all``."""
    repo = _init_hermetic_repo(tmp_path, branch="dev")
    mocker.patch("app.size_limit_policy._REPO_ROOT", repo)
    assert slp._mainline_git_ref() == "HEAD"


def test_truncation_probe_failure_fails_closed(tmp_path, mocker):
    """A shallow checkout whose ancestry probe CANNOT run (git exits 128 —
    the containerized dubious-ownership shape) must read as TRUNCATED.

    Regression: the probe used to treat any non-zero ``merge-base
    --is-ancestor`` exit as "boundary not in ancestry" and fall through to
    NOT-truncated, so a depth-1 CI checkout bind-mounted into a root container
    evaluated the full-history ledger guard against partial history and
    reported phantom ledger drift (git showed [] for bumps that mainline
    history genuinely contains)."""
    checkout = _init_shallow_repo(tmp_path)

    real_run = subprocess.run

    def _dubious(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "merge-base" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: detected dubious ownership")
        return real_run(cmd, *args, **kwargs)

    mocker.patch("app.size_limit_policy.subprocess.run", side_effect=_dubious)
    assert slp._git_history_is_truncated(checkout) is True, (
        "an unprovable shallow boundary must fail closed (truncated), never open"
    )


def test_truncation_probe_exception_fails_closed(tmp_path, mocker):
    """If the ancestry probe cannot even spawn (git missing mid-flight), a
    present shallow boundary must still read as truncated."""
    checkout = _init_shallow_repo(tmp_path)

    real_run = subprocess.run

    def _boom(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "merge-base" in cmd:
            raise OSError("git unavailable")
        return real_run(cmd, *args, **kwargs)

    mocker.patch("app.size_limit_policy.subprocess.run", side_effect=_boom)
    assert slp._git_history_is_truncated(checkout) is True
