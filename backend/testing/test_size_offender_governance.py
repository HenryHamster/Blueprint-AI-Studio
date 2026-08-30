"""Tests for chronic module-size-offender governance.

Covers:
- ``_is_chronic_offender`` threshold logic (window and growth triggers)
- ``_parse_git_diff_output`` diff parsing
- ``_emit_size_violation_alerts`` operator-alert suppression
- Repeated chronic and first-time violations remain strategist-internal
- Acceptance criterion: ``test_chronic_offender_escalates_not_silently_bumped``
"""

from __future__ import annotations

import ast
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


@pytest.mark.parametrize(
    ("module_key", "source_path"),
    (
        (
            "release_prepromotion_gradeability.py",
            "backend/app/release_prepromotion_gradeability.py",
        ),
        ("release_activation_receipt.py", "backend/app/release_activation_receipt.py"),
        ("release_authority_lock.py", "backend/app/release_authority_lock.py"),
        (
            "release_qualification_authority.py",
            "backend/app/release_qualification_authority.py",
        ),
        (
            "release_qualification_observation_protocol.py",
            "backend/app/release_qualification_observation_protocol.py",
        ),
        ("release_serving_observation.py", "backend/app/release_serving_observation.py"),
    ),
)
def test_new_release_modules_have_paired_governing_cap_repairs(
    module_key,
    source_path,
):
    assert slp.governing_cap_repair("backend", module_key, 999) == {
        "source_path": source_path,
        "constant_name": "_MODULE_SIZE_GOVERNANCE_CAP",
        "new_limit": 999,
    }


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


@pytest.mark.parametrize(
    ("count", "cap", "base_count", "expected"),
    [
        (1201, 1200, None, True),
        (1201, 1200, 1200, True),
        (1201, 1200, 1201, False),
        (1201, 1200, 1300, False),
        (1200, 1200, 1100, False),
    ],
)
def test_is_breach_attributes_only_new_or_grown_breaches(
    count, cap, base_count, expected
):
    """A baseline suppresses inherited debt but not a new/grown breach."""
    assert slp.is_breach(count, cap, base_count=base_count) is expected


def test_is_breach_missing_baseline_fails_closed():
    """No baseline evidence keeps an absolute breach blocking."""
    assert slp.is_breach(1201, 1200, base_count=None)


@pytest.mark.parametrize(
    ("base_count", "base_effective_cap", "expected"),
    [
        (None, 1200, None),
        (1200, None, None),
        (1200, 1200, None),
        (1199, 1200, None),
        (1201, 1200, 1201),
    ],
)
def test_eligible_baseline_count_requires_a_base_tree_breach(
    base_count, base_effective_cap, expected
):
    """The shared policy permits only a base-side effective-cap breach."""
    assert (
        slp.eligible_baseline_count(base_count, base_effective_cap) == expected
    )


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
    records = _parse_git_diff_output(_FIXTURE_GIT_LOG, "reaper_sweeps.py", "backend")
    assert len(records) == 2
    assert records[0]["old_cap"] == 3500
    assert records[0]["new_cap"] == 4050
    assert records[1]["old_cap"] == 4050
    assert records[1]["new_cap"] == 4700
    # sorted oldest first
    assert records[0]["commit_date"] < records[1]["commit_date"]


def test_parse_git_diff_ignores_other_keys():
    records = _parse_git_diff_output(_FIXTURE_GIT_LOG, "orchestrator.py", "backend")
    assert records == []


def test_parse_git_diff_ignores_decreases():
    log_with_decrease = """\
COMMIT ccc333 2024-05-01T09:00:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -5,7 +5,7 @@
   "backend": {
-    "tiny_module.py": 5000,
+    "tiny_module.py": 100,
   }
"""
    records = _parse_git_diff_output(log_with_decrease, "tiny_module.py", "backend")
    # Decrease (5000 → 100) is NOT a bump; should be excluded.
    assert records == []


def test_parse_git_diff_empty_output():
    assert _parse_git_diff_output("", "anything.py", "backend") == []


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
    """Return a path to a repo git can actually READ from here, or None.

    Presence of ``.git`` is not the predicate, in either direction:

    * ``os.path.isdir`` was the original check, and in a LINKED WORKTREE
      ``.git`` is a FILE holding ``gitdir: <path>`` -- so it silently returned
      None and skipped this guard in every agent and dev worktree. It only ever
      ran in a real clone, and a green local run proved nothing about it.
    * ``os.path.exists`` fixes that but overshoots: the standalone lane mounts
      the checkout at ``/repo`` inside a container, and a worktree's ``gitdir:``
      line names a HOST path that does not exist there. git then dies with
      ``fatal: not a git repository: /repo/D:/.../.git/worktrees/<name>`` and
      the guard errors instead of skipping.

    So ask git, with the cheapest question that fails exactly when the later
    ``git log`` would: reachable gitdir, readable object store, resolvable HEAD.
    """
    candidates = [slp._REPO_ROOT]
    if os.path.exists("/repo/.git"):
        candidates.append("/repo")
    for root in candidates:
        if not os.path.exists(os.path.join(root, ".git")):
            continue
        try:
            probe = subprocess.run(
                ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                # Comfortably under MAX_INNER_OPERATION_SECONDS (backend/
                # testing/test_harness_timeout_budgets.py): this reads one ref.
                # A budget that could not fire is the bug class that module
                # guards. Sized against the CEILING, not the lane's
                # LANE_HANG_DETECTOR_SECONDS catch-all.
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip():
            return root
    return None


# `git log -p -- backend/testing/module_size_limits.json` materialises a diff
# for every commit that ever touched a large, constantly-churned file, on a
# runner already carrying eight parallel shards. The inner subprocess budget
# below is 30s, which is exactly MAX_INNER_OPERATION_SECONDS
# (test_harness_timeout_budgets.py) -- an inner call AT the ceiling, so without
# the marker below any work before or after it guarantees the harness kill lands
# first and this guard reds as an unreproducible flake (CI run 32805982004,
# backend shard 8, when the lane budget was also 30s).
#
# 120s, down from a deliberately oversized 900s. The `git log -p` above is
# NOT where the time went -- measured, it is ~1s. The cost was
# `phantom_ledger_records` forking git FOUR TIMES PER LEDGER RECORD for every
# SHA the (shallow, on CI) log cannot serve: ~2,660 spawns, measured at
# 683.69s on a truncated clone. #17888 batched those probes into a constant
# number of forks -- measured 5.37s over the same 714 records with an
# element-wise identical phantom list -- so the whole file now runs in 15.49s.
# 120s is ~8x headroom over the file and ~22x over this test; it stays a marker
# because the inner call sits at MAX_INNER_OPERATION_SECONDS and a shallow-clone
# cold cache is still slower than a warm one.
@pytest.mark.timeout(120)
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
        pytest.skip(
            "no git-readable checkout of this repo is reachable from here, so the "
            "ledger guard has nothing to compare against. Most often a LINKED "
            "WORKTREE mounted into the standalone lane's container: its .git file "
            "names a host gitdir that does not exist inside the container. CI runs "
            "this guard in a real clone, where it executes rather than skipping."
        )
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
            # Whole-file context: a cap's identity is (section, key), and the
            # enclosing ``"<section>": {`` opener is outside a default -U3 hunk.
            # Without this the guard reads a bump of testing/X.py as a bump of
            # scripts/X.py -- the exact collision it is here to catch.
            slp._FULL_CONTEXT_DIFF_ARG,
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
    # Parsed ONCE for every (section, key): the full-context stream is
    # multi-megabyte and this loop walks several hundred caps.
    git_by_section_key = slp._parse_git_log_bump_records(git_out)
    mismatches = []
    for section, keys in caps.items():
        if not isinstance(keys, dict):
            continue
        for key in keys:
            git_records = git_by_section_key.get((section, key), [])
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
        real_run = subprocess.run
        git_calls = []

        def record_git_call(args, *call_args, **call_kwargs):
            if args and args[0] == "git":
                git_calls.append(args)
            return real_run(args, *call_args, **call_kwargs)

        mocker.patch.object(slp.subprocess, "run", side_effect=record_git_call)
        records = slp._read_git_bump_history("m.py", section="backend")
        assert records is not None
        assert [(r["old_cap"], r["new_cap"]) for r in records] == [(100, 200)]
        assert ["-c", f"safe.directory={repo}"] == git_calls[-1][1:3]

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
        all_records = slp._parse_git_diff_output(all_out, "m.py", "backend")
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


def test_governance_bound_breach_names_the_cap_that_actually_bound() -> None:
    """A governed breach reports the governance cap, not the looser JSON cap.

    The gate used to print the JSON number for every breach, so a module
    stopped by its own source-declared cap produced an arithmetically false
    comparison ("1979 > 2250") and pointed the reader at module_size_limits.json
    -- the one file whose edit could not clear the block.
    """
    message = slp.describe_effective_cap_breach(
        "backend", "approved_pr_stranding.py", 1979, 2250, 1978
    )

    assert "1979 > 1978" in message
    assert "1979 > 2250" not in message
    assert "backend/app/approved_pr_stranding.py" in message
    assert "2250" in message and "NOT the binding limit" in message


def test_json_bound_breach_reports_the_json_cap_plainly() -> None:
    """With no tighter governance cap the effective cap IS the JSON cap."""
    message = slp.describe_effective_cap_breach(
        "backend", "reaper.py", 5001, 5000, 5000
    )

    assert message == "reaper.py (5001 > 5000)"


def test_unresolvable_cap_says_so_instead_of_printing_none() -> None:
    """A module with no resolvable cap reports that, not "> None"."""
    message = slp.describe_effective_cap_breach(
        "backend", "mystery.py", 42, None, None
    )

    assert "no cap resolved" in message
    assert "None" not in message


# Literal pins. Never replace these with the constants they check.
_SAFE_DIRECTORY_OPTION = "safe.directory="
_GIT_INVOCATIONS_FLOOR = 16


def test_every_literal_git_argv_in_the_size_guard_is_safe_directory_hardened():
    """Every literal ``git`` argv in the size-guard modules carries ``-c safe.directory=``.

    The CI backend shards bind the checkout read-only at /repo into a ROOT
    container while the runner user owns the files (``-v "$PWD:/repo:ro"``, no
    ``--user``), so an unhardened git call exits 128 with "dubious ownership".

    Promotion PR #17914 went red on exactly that: ``_read_git_bump_history``
    lacked the option its own ref probe in ``_mainline_git_ref`` already
    carried, so the probe resolved a mainline ref and the read then died --
    read as genuine evidence loss, refusing every cap bump in the shard.  A
    SECOND read, in ``rebuild_cap_bump_history_ledger``, lacked it too but
    fails OPEN: it would have gone on silently reconciling the ledger from zero
    git evidence and never reddened anything.

    So pin the whole module surface rather than those two call sites.  A
    partially hardened module is the defect; "hardened the probe but not the
    read it gates" is the class.
    """
    unhardened: list[tuple[str, int, str]] = []
    scanned = 0
    for module in (slp, csl):
        path = Path(module.__file__)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) not in ("subprocess.run", "subprocess.check_output"):
                continue
            argv = node.args[0] if node.args else None
            parts = [ast.unparse(elt) for elt in getattr(argv, "elts", None) or []]
            if not parts or "git" not in parts[0]:
                continue
            scanned += 1
            if not any(_SAFE_DIRECTORY_OPTION in part for part in parts):
                unhardened.append((path.name, node.lineno, " ".join(parts[:6])))

    # Guard the guard: a scan that silently matched nothing would pass vacuously.
    assert scanned >= _GIT_INVOCATIONS_FLOOR, (
        f"only {scanned} literal git invocations found across "
        f"{[Path(m.__file__).name for m in (slp, csl)]}; the scan itself is broken"
    )
    assert not unhardened, (
        "git invocations missing -c safe.directory= (they exit 128 under CI's "
        f"root-container /repo bind mount): {unhardened}"
    )


def test_per_root_git_probes_do_not_rescale_with_module_count(tmp_path, mocker):
    """One checkout is probed ONCE, however many modules are classified.

    ``parse_bump_history`` is cached per ``(module_key, section, root)``, but the
    two git probes it performs -- ``_mainline_git_ref`` and
    ``_git_history_is_truncated`` -- answer a question about the CHECKOUT, not
    about any one module.  Before the per-root memo every additional module
    re-shelled both: SIX ``git`` process spawns per module (1 ``rev-parse
    --git-path``, up to 4 ``rev-parse --verify`` for the mainline candidates,
    1 ``merge-base --is-ancestor``).

    ``app.metrics_integrity_sweep.check_near_cap_size_warning`` calls
    ``parse_bump_history`` once per near-cap module -- 48 of them today -- from
    inside a single ``asyncio.to_thread`` in the reaper cycle, so ONE cycle
    spawned ~288 git processes serially.  Measured at 9.3s on an idle dev box
    and 16.1s inside a CI shard container, against that lane's then-30s per-test
    budget; ordinary host jitter pushed it over and red-lit promotion PR #17977
    (``TestReaperService::test_recent_heartbeat_task_is_not_reaped``, nominal
    duration 0.68s).

    The pin is on the SHAPE, not on a wall-clock number, so it cannot go
    vacuous on a fast machine: probe spawns must be CONSTANT in the module
    count.  Deleting the memo makes the second loop spawn 6 git processes per
    module again and this assertion reports the exact excess.
    """
    repo = _init_shallow_repo(tmp_path)
    slp._bump_history_cache.clear()
    slp._git_probe_cache.clear()

    git_calls: list[list[str]] = []
    real_run = subprocess.run

    def _record(args, *call_args, **call_kwargs):
        if args and args[0] == "git":
            git_calls.append(list(args))
        return real_run(args, *call_args, **call_kwargs)

    mocker.patch.object(slp.subprocess, "run", side_effect=_record)
    with patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        slp.parse_bump_history("m.py", "backend", repo)
        first_module_spawns = len(git_calls)
        for i in range(9):
            slp.parse_bump_history(f"other{i}.py", "backend", repo)
        extra = len(git_calls) - first_module_spawns

    # Guard the guard: if the first classification shelled out to git zero
    # times the fixture is not exercising the git path at all and the
    # constant-cost assertion below would pass vacuously.
    assert first_module_spawns > 0, (
        "the fixture checkout never reached a git probe, so this test would "
        "pass no matter how the probes are cached"
    )
    assert extra == 0, (
        f"classifying 9 further modules against the SAME root spawned {extra} "
        f"extra git processes ({extra / 9:.1f} per module) on top of the "
        f"{first_module_spawns} the first module paid. The per-root probes must "
        f"be memoised, or probe cost scales with the number of modules a single "
        f"sweep classifies -- the 288-spawn reaper stall this test pins."
    )


def test_governance_cap_prefilter_is_semantics_preserving():
    """The substring prefilter must never change what a scan discovers.

    ``governance_caps_from_source_text`` screens source text for the literal
    ``_SIZE_GOVERNANCE_CAP`` before paying for ``ast.parse``. That is the
    difference between a 21.25s and a 1.86s five-section live-tree scan, which
    is what makes ``test_module_size_live_tree.py`` cheap enough to be
    mandatory on every impacted run. It is safe only because every declaration
    the parser can recognise binds a NAME ending in that literal, so the screen
    and the matcher must stay the SAME string.
    Both are read structurally out of the function's own AST and pinned against
    a literal written here, so a rename cannot certify itself.
    """
    func = next(
        node
        for node in ast.walk(ast.parse(Path(slp.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        and node.name == "governance_caps_from_source_text"
    )
    # The screen: a bare ``"..." in source_text`` membership test.
    screens = {
        node.left.value
        for node in ast.walk(func)
        if isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
    }
    # The matcher: the argument to the ``target.id.endswith(...)`` call.
    matchers = {
        node.args[0].value
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "endswith"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    literals = screens | matchers
    assert screens and matchers, (
        "expected governance_caps_from_source_text to keep both a substring "
        f"screen and an endswith() matcher; found screens={sorted(screens)} "
        f"matchers={sorted(matchers)}"
    )
    assert literals == {"_SIZE_GOVERNANCE_CAP"}, (
        "the prefilter screen and the endswith() name matcher must use the SAME "
        f"literal; found {sorted(literals)}. A screen stricter than the matcher "
        "silently stops discovering caps and the size gate goes vacuous."
    )

    # Recognised forms still parse, with and without an annotation.
    assert slp.governance_caps_from_source_text(
        "_MODULE_SIZE_GOVERNANCE_CAP = 900\n"
    ) == [900]
    assert slp.governance_caps_from_source_text(
        "_MODULE_SIZE_GOVERNANCE_CAP: int = 900\n"
    ) == [900]

    # A file with no declaration is screened out -- same answer as before.
    assert slp.governance_caps_from_source_text("x = 1\n") == []

    # The prefilter is a SUPERSET screen, never a matcher: source that carries
    # the literal only in a comment or an unrelated name still parses and still
    # yields nothing, so the screen cannot manufacture a cap either.
    assert slp.governance_caps_from_source_text(
        "# _SIZE_GOVERNANCE_CAP is documented here\nx = 1\n"
    ) == []
    assert slp.governance_caps_from_source_text(
        "_SIZE_GOVERNANCE_CAPS_BY_NAME = {'a': 5}\n"
    ) == []

    # Syntactically invalid source that DOES carry the literal still degrades
    # to [] rather than raising past the prefilter.
    assert slp.governance_caps_from_source_text(
        "_MODULE_SIZE_GOVERNANCE_CAP = (\n"
    ) == []


# ---------------------------------------------------------------------------
# Section-collision attribution.
#
# ``module_size_limits.json`` maps a SECTION to ``{module_basename: cap}``, and
# the same basename legitimately exists in two sections pointing at two
# DIFFERENT real files: ``test_local_runner.py`` is both a ``testing`` cap and
# a ``scripts`` cap.  The git-history parse used to key bumps by basename
# alone, with no idea which section object enclosed the diff line.  That was
# wrong in BOTH directions, and both are pinned here:
#
# (a) FALSE FAIL — a bump of ``testing/X.py`` was reported as unrecorded drift
#     in ``scripts/X.py``.  The checker exited 1 on a correct ledger and its
#     ``--fix`` wrote a record for a transition that never happened.
# (b) FALSE ACCEPT — a ledger record filed under section A could be
#     "justified" by an unrelated bump in section B carrying a coincidentally
#     equal ``(old_cap, new_cap)`` pair, so a fabricated record survived the
#     phantom guard.  This is the direction that actually protects the guard;
#     the live ledger had exactly one such record.
#
# Attribution needs the enclosing ``"<section>": {`` opener, which is outside
# a default three-line diff hunk — hence ``_FULL_CONTEXT_DIFF_ARG`` on every
# read of the caps file.
# ---------------------------------------------------------------------------

# Literal pin: the real basename that collides across sections today. Keeping
# it literal means a future rename cannot quietly make these tests vacuous.
_COLLIDING_BASENAME = "test_local_runner.py"


def _two_section_repo(tmp_path, revisions, name="collide"):
    """Hermetic repo whose caps file holds ONE basename in TWO sections.

    ``revisions`` is a list of ``(testing_cap, scripts_cap, iso_date)`` written
    and committed in order.  Returns ``(repo, caps_path, [sha, ...])``.
    """
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    caps_path = repo / "module_size_limits.json"
    shas: list[str] = []
    for testing_cap, scripts_cap, date in revisions:
        caps_path.write_text(
            json.dumps(
                {
                    "_doc": "fixture",
                    "testing": {_COLLIDING_BASENAME: testing_cap},
                    "scripts": {_COLLIDING_BASENAME: scripts_cap},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(
            ["git", "add", "module_size_limits.json"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", f"caps {testing_cap}/{scripts_cap}"],
            cwd=repo,
            env=env,
            check=True,
        )
        shas.append(_hermetic_head_sha(str(repo)))
    return str(repo), str(caps_path), shas


def _write_ledger(tmp_path, history, name="cap_bump_history.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"schema_version": 1, "history": history}, indent=2),
        encoding="utf-8",
    )
    return str(path)


def test_scan_diff_bumps_attributes_a_shared_basename_to_its_own_section():
    """Two sections, one basename, one changed: only that section gets the pair."""
    diff = (
        "diff --git a/module_size_limits.json b/module_size_limits.json\n"
        "--- a/module_size_limits.json\n"
        "+++ b/module_size_limits.json\n"
        "@@ -1,9 +1,9 @@\n"
        " {\n"
        '   "testing": {\n'
        f'     "{_COLLIDING_BASENAME}": 15100\n'
        "   },\n"
        '   "scripts": {\n'
        f'-    "{_COLLIDING_BASENAME}": 14250\n'
        f'+    "{_COLLIDING_BASENAME}": 16650\n'
        "   }\n"
        " }\n"
    )
    scanned = slp._scan_diff_bumps(diff)
    assert scanned.by_section_key == {
        ("scripts", _COLLIDING_BASENAME): {(14250, 16650)}
    }
    assert ("testing", _COLLIDING_BASENAME) not in scanned.by_section_key
    assert scanned.unattributed_keys == frozenset()


def test_scan_diff_bumps_fails_open_when_the_section_is_not_in_the_hunk():
    """No enclosing section opener: evidence is UNATTRIBUTED, never guessed.

    A default ``-U3`` hunk does not carry the section opener.  Attributing such
    a line to some default section is the defect; reporting it as absent
    evidence is the safe direction for BOTH halves of the ledger guard.
    """
    diff = (
        "@@ -340,7 +340,7 @@\n"
        f'-    "{_COLLIDING_BASENAME}": 15100,\n'
        f'+    "{_COLLIDING_BASENAME}": 19700,\n'
    )
    scanned = slp._scan_diff_bumps(diff)
    assert scanned.by_section_key == {}
    assert scanned.unattributed_keys == frozenset({_COLLIDING_BASENAME})


def test_scan_diff_bumps_tracks_old_and_new_section_cursors_independently():
    """A renamed section: the ``-`` entry is old-side, the ``+`` entry new-side."""
    diff = (
        "@@ -1,5 +1,5 @@\n"
        " {\n"
        '-  "old_section": {\n'
        f'-    "{_COLLIDING_BASENAME}": 100\n'
        '+  "new_section": {\n'
        f'+    "{_COLLIDING_BASENAME}": 200\n'
        "   }\n"
        " }\n"
    )
    scanned = slp._scan_diff_bumps(diff)
    # Neither side sees BOTH an old and a new value, so neither is a bump —
    # a section rename is not a cap raise and must not be reported as one.
    assert scanned.by_section_key == {}
    assert scanned.unattributed_keys == frozenset()


def test_caps_file_diff_reads_request_whole_file_context():
    """Every read of the caps file must render full context.

    Literal pin, deliberately not the constant it checks: with git's default
    three context lines the enclosing ``"<section>": {`` opener is outside the
    hunk (git's funcname heuristic finds nothing in JSON either), so every bump
    would be unattributable and the guard would silently stop looking — the
    "could not look, then reported clean" class of #17872/#17920.
    """
    assert slp._FULL_CONTEXT_DIFF_ARG == "-U100000"
    source = Path(slp.__file__).read_text(encoding="utf-8")
    # Every ``git log -p`` argv in the module must be followed immediately by
    # the full-context argument, so a new read cannot be added section-blind.
    log_renders = source.split('"-p",')[1:]
    assert len(log_renders) >= 4, len(log_renders)
    for chunk in log_renders:
        assert chunk.lstrip().startswith("_FULL_CONTEXT_DIFF_ARG,"), chunk[:120]
    # ``git show`` renders the caps-file diff too and needs the same context.
    show_renders = source.split('"show",')[1:]
    sha_renders = [c for c in show_renders if c.lstrip().startswith("sha,")]
    assert len(sha_renders) == 1, len(sha_renders)
    assert sha_renders[0].split("sha,", 1)[1].lstrip().startswith(
        "_FULL_CONTEXT_DIFF_ARG,"
    )


def test_missing_ledger_bumps_are_not_attributed_to_another_section(tmp_path):
    """(a) FALSE FAIL: a testing/ bump must never be charged to scripts/.

    Mutation proof: with the section-blind parse this returns TWO entries — the
    real ``testing`` bump AND a fabricated ``scripts`` one for the same pair,
    which is the ``[ledger-missing] scripts/test_local_runner.py`` line the
    checker emitted against a correct ledger.
    """
    repo, caps_path, shas = _two_section_repo(
        tmp_path,
        [
            (15100, 16650, "2026-01-01T00:00:00+00:00"),
            # ONLY the testing cap moves; scripts stays at 16650 throughout.
            (19700, 16650, "2026-01-02T00:00:00+00:00"),
        ],
    )
    ledger_path = _write_ledger(tmp_path, {})

    with patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        missing = slp.missing_ledger_bump_records(
            repo_root=repo, caps_json_path=caps_path, ledger_path=ledger_path
        )

    assert [entry[0] for entry in missing if entry[0] == "scripts"] == []
    assert len(missing) == 1, missing
    section, key, old_cap, new_cap, commit_sha, _date, status = missing[0]
    assert (section, key, old_cap, new_cap) == (
        "testing",
        _COLLIDING_BASENAME,
        15100,
        19700,
    )
    assert commit_sha == shas[1]
    assert status == slp.LEDGER_BUMP_ABSENT


def test_missing_ledger_bumps_clean_when_only_the_right_section_is_recorded(tmp_path):
    """The same history with a correct ledger must produce NO findings.

    This is the end-to-end shape of the false failure: the ledger records the
    testing bump and nothing else, which is the truth, yet the section-blind
    parse still demanded a ``scripts`` record for a transition that never
    happened — and ``--fix`` would have written it.
    """
    repo, caps_path, shas = _two_section_repo(
        tmp_path,
        [
            (15100, 16650, "2026-01-01T00:00:00+00:00"),
            (19700, 16650, "2026-01-02T00:00:00+00:00"),
        ],
    )
    ledger_path = _write_ledger(
        tmp_path,
        {
            "testing": {
                _COLLIDING_BASENAME: [
                    {
                        "commit_sha": shas[1],
                        "commit_date": "2026-01-02T00:00:00+00:00",
                        "old_cap": 15100,
                        "new_cap": 19700,
                    }
                ]
            }
        },
    )

    with patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        missing = slp.missing_ledger_bump_records(
            repo_root=repo, caps_json_path=caps_path, ledger_path=ledger_path
        )

    assert missing == []


def test_phantom_ledger_rejects_a_cross_section_justification(tmp_path):
    """(b) FALSE ACCEPT: section B's bump must not justify section A's record.

    The security-relevant direction.  The commit raises ``scripts/X.py``
    14250 -> 16650 and leaves ``testing/X.py`` untouched, but the ledger files
    that pair under ``testing``.  With the section-blind parse the commit's
    diff "showed" the pair for the bare basename, the fabricated record was
    accepted, and the deployed chronic-offender classifier counted a bump that
    never happened for that file.  The live ledger held exactly this record.

    Mutation proof: the unfixed parser returns ``[]`` here.
    """
    repo, _caps_path, shas = _two_section_repo(
        tmp_path,
        [
            (15100, 14250, "2026-01-01T00:00:00+00:00"),
            # ONLY the scripts cap moves; testing stays at 15100 throughout.
            (15100, 16650, "2026-01-02T00:00:00+00:00"),
        ],
    )
    ledger = {
        "schema_version": 1,
        "history": {
            "testing": {
                _COLLIDING_BASENAME: [
                    {
                        "commit_sha": shas[1],
                        "commit_date": "2026-01-02T00:00:00+00:00",
                        "old_cap": 14250,
                        "new_cap": 16650,
                    }
                ]
            }
        },
    }

    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        phantoms = slp.phantom_ledger_records(repo_root=repo, history=ledger)

    assert len(phantoms) == 1, phantoms
    section, key, old_cap, new_cap, sha, reason = phantoms[0]
    assert (section, key, old_cap, new_cap, sha) == (
        "testing",
        _COLLIDING_BASENAME,
        14250,
        16650,
        shas[1],
    )
    assert "no recorded bump" in reason


def test_phantom_ledger_accepts_the_same_pair_filed_under_the_right_section(tmp_path):
    """Control for the test above: the record IS justified under ``scripts``.

    Without this, "flag everything" would pass the false-accept test.  The
    identical pair and commit, filed under the section that actually moved,
    must survive.
    """
    repo, _caps_path, shas = _two_section_repo(
        tmp_path,
        [
            (15100, 14250, "2026-01-01T00:00:00+00:00"),
            (15100, 16650, "2026-01-02T00:00:00+00:00"),
        ],
    )
    ledger = {
        "schema_version": 1,
        "history": {
            "scripts": {
                _COLLIDING_BASENAME: [
                    {
                        "commit_sha": shas[1],
                        "commit_date": "2026-01-02T00:00:00+00:00",
                        "old_cap": 14250,
                        "new_cap": 16650,
                    }
                ]
            }
        },
    }

    with patch("app.size_limit_policy._REPO_ROOT", repo), patch(
        "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH", "module_size_limits.json"
    ):
        phantoms = slp.phantom_ledger_records(repo_root=repo, history=ledger)

    assert phantoms == []


def test_parse_bump_history_does_not_borrow_another_sections_bumps(tmp_path):
    """The production history read is section-scoped too.

    ``parse_bump_history`` feeds the chronic-offender classifier.  A
    section-blind read gave ``testing/X.py`` the bump history of
    ``scripts/X.py`` — a file it has nothing to do with — which is how a
    routine first bump could be refused as chronic (or vice versa).
    """
    repo, _caps_path, _shas = _two_section_repo(
        tmp_path,
        [
            (15100, 14250, "2026-01-01T00:00:00+00:00"),
            (15100, 16650, "2026-01-02T00:00:00+00:00"),
        ],
    )

    slp._bump_history_cache.clear()
    try:
        with patch(
            "app.size_limit_policy._MODULE_SIZE_LIMITS_RELPATH",
            "module_size_limits.json",
        ):
            testing_history = slp.parse_bump_history(
                _COLLIDING_BASENAME, "testing", repo
            )
            scripts_history = slp.parse_bump_history(
                _COLLIDING_BASENAME, "scripts", repo
            )
    finally:
        slp._bump_history_cache.clear()

    assert testing_history == []
    assert [(r["old_cap"], r["new_cap"]) for r in scripts_history] == [(14250, 16650)]


# ---------------------------------------------------------------------------
# Operator-approval justification on ledger records.
#
# ``--force-chronic`` used to gate on the mere PRESENCE of
# ``SIZE_CAP_OPERATOR_APPROVAL``: the value was never read or persisted, so a
# chronic bump left no durable trace of who authorised it or why -- only a
# commit message nothing validates and a squash can lose.  It is now stored as
# the record's ``justification``.  Three load-bearing properties are pinned
# here: the field is OPTIONAL (legacy records keep validating and are never
# backfilled with a fabricated reason), it does not disturb the
# (old_cap, new_cap) ratchet match, and it SURVIVES the git regeneration a
# later ``--fix`` performs.
# ---------------------------------------------------------------------------


_TEST_JUSTIFICATION = "henry: m.py holds the promotion gate; decomposition is queued"


def test_legacy_ledger_record_without_justification_still_validates(tmp_path, mocker):
    """Absent means absent: a pre-field record parses AND justifies its raise.

    Hundreds of committed records predate the field (184 written on 2026-08-26
    alone).  They keep validating untouched -- no backfill with an invented
    reason -- and keep satisfying the ratchet, which matches the exact pair.
    """
    slp._bump_history_cache.clear()
    legacy = {
        "commit_sha": "abc123def456",
        "commit_date": "2026-08-26T00:00:00+00:00",
        "old_cap": 100,
        "new_cap": 200,
    }
    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps({"schema_version": 1, "history": {"backend": {"m.py": [legacy]}}}),
        encoding="utf-8",
    )
    _deployed_layout(tmp_path, mocker, ledger_path=str(ledger_path))

    history = parse_bump_history("m.py", "backend")

    assert [(r["old_cap"], r["new_cap"]) for r in history] == [(100, 200)]
    assert csl._record_justifies(history[0], 100, 200)
    assert csl._record_justifies(legacy, 100, 200)
    # The reader must not have invented a value for the missing field.
    on_disk = json.loads(ledger_path.read_text())["history"]["backend"]["m.py"][0]
    assert "justification" not in on_disk


def test_ledger_record_with_justification_still_validates_and_justifies(
    tmp_path, mocker
):
    """The new field is inert to every existing consumer of a record."""
    slp._bump_history_cache.clear()
    recorded = {
        "commit_sha": "abc123def456",
        "commit_date": "2026-08-26T00:00:00+00:00",
        "old_cap": 100,
        "new_cap": 200,
        "justification": _TEST_JUSTIFICATION,
    }
    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps({"schema_version": 1, "history": {"backend": {"m.py": [recorded]}}}),
        encoding="utf-8",
    )
    _deployed_layout(tmp_path, mocker, ledger_path=str(ledger_path))

    history = parse_bump_history("m.py", "backend")

    assert [(r["old_cap"], r["new_cap"]) for r in history] == [(100, 200)]
    assert csl._record_justifies(recorded, 100, 200)
    # ...and only the exact pair, so the ratchet is unchanged by the new key.
    assert not csl._record_justifies(recorded, 100, 300)


def test_ledger_with_pending_bumps_preserves_a_recorded_justification():
    """A re-staged bump must not erase the approval already on its record."""
    ledger = {
        "schema_version": 1,
        "history": {
            "backend": {
                "m.py": [
                    {
                        "commit_sha": UNATTRIBUTED_COMMIT_SHA,
                        "commit_date": "2026-01-01T00:00:00+00:00",
                        "old_cap": 100,
                        "new_cap": 200,
                        "justification": _TEST_JUSTIFICATION,
                    }
                ]
            }
        },
    }

    deduped = slp.ledger_with_pending_bumps(ledger, [("backend", "m.py", 100, 200)])
    attributed = slp.ledger_with_pending_bumps(
        ledger,
        [("backend", "m.py", 100, 200, "realsha", "2026-02-03T04:05:06+00:00")],
    )

    assert deduped["history"]["backend"]["m.py"][0]["justification"] == (
        _TEST_JUSTIFICATION
    )
    upgraded = attributed["history"]["backend"]["m.py"][0]
    assert upgraded["commit_sha"] == "realsha"
    assert upgraded["justification"] == _TEST_JUSTIFICATION


def test_apply_ledger_justifications_never_overwrites_or_invents():
    """Stamping is additive only: first authorisation wins, absent stays absent."""
    from app.size_limit_justification import apply_ledger_justifications

    ledger = {
        "history": {
            "backend": {
                "m.py": [
                    {
                        "commit_sha": "s1",
                        "commit_date": "2026-01-01T00:00:00+00:00",
                        "old_cap": 100,
                        "new_cap": 200,
                        "justification": _TEST_JUSTIFICATION,
                    },
                    {
                        "commit_sha": "s2",
                        "commit_date": "2026-01-02T00:00:00+00:00",
                        "old_cap": 200,
                        "new_cap": 300,
                    },
                ]
            }
        }
    }

    result = apply_ledger_justifications(
        ledger,
        None,
        {("backend", "m.py", 100, 200): "someone else: a later, different reason"},
    )
    records = result["history"]["backend"]["m.py"]

    assert records[0]["justification"] == _TEST_JUSTIFICATION
    assert "justification" not in records[1]


def test_rebuild_carries_a_recorded_justification_across_git_regeneration(
    tmp_path, mocker
):
    """The durability hole this field exists to close.

    On a complete checkout ``rebuild_cap_bump_history_ledger`` rebuilds every
    record from ``git log -p``, which knows only the four cap fields, so
    without a carry-forward the approval would be dropped by the next
    ``--fix`` -- persisted in name only.
    """
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "bump to 200", date="2026-01-02T00:00:00+00:00")
    bump_sha = _hermetic_head_sha(repo)

    recorded = {
        "commit_sha": bump_sha,
        "commit_date": "2026-01-02T00:00:00+00:00",
        "old_cap": 100,
        "new_cap": 200,
        "justification": _TEST_JUSTIFICATION,
    }
    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps({"schema_version": 1, "history": {"backend": {"m.py": [recorded]}}}),
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
    assert records[0]["justification"] == _TEST_JUSTIFICATION


def test_rebuild_stamps_a_new_operator_justification_on_its_pending_bump(
    tmp_path, mocker
):
    """``justifications`` reaches the record the run just staged, and no other."""
    caps_path = tmp_path / "module_size_limits.json"
    caps_path.write_text(
        json.dumps({"backend": {"m.py": 300, "other.py": 300}}), encoding="utf-8"
    )
    ledger_path = tmp_path / "cap_bump_history.json"
    ledger_path.write_text(
        json.dumps({"schema_version": 1, "history": {}}), encoding="utf-8"
    )
    mocker.patch("app.size_limit_policy._git_reads_history_here", return_value=False)

    ledger = slp.rebuild_cap_bump_history_ledger(
        caps_json_path=str(caps_path),
        ledger_path=str(ledger_path),
        pending=[("backend", "m.py", 200, 300), ("backend", "other.py", 200, 300)],
        justifications={("backend", "m.py", 200, 300): _TEST_JUSTIFICATION},
    )

    on_disk = json.loads(ledger_path.read_text())["history"]["backend"]
    assert ledger["history"]["backend"]["m.py"][0]["justification"] == (
        _TEST_JUSTIFICATION
    )
    assert on_disk["m.py"][0]["justification"] == _TEST_JUSTIFICATION
    assert set(on_disk["m.py"][0]) == {
        "commit_sha",
        "commit_date",
        "old_cap",
        "new_cap",
        "justification",
    }
    # An unapproved bump staged in the same run stays unjustified.
    assert "justification" not in on_disk["other.py"][0]


def test_phantom_ledger_records_ignores_the_justification_field(tmp_path, mocker):
    """The reverse ledger guard adjudicates cap pairs, not the new key."""
    repo = _init_hermetic_repo(tmp_path)
    _hermetic_write_cap(repo, 100)
    _hermetic_commit(repo, "init", date="2026-01-01T00:00:00+00:00")
    _hermetic_write_cap(repo, 200)
    _hermetic_commit(repo, "bump to 200", date="2026-01-02T00:00:00+00:00")
    bump_sha = _hermetic_head_sha(repo)

    justified = {
        "commit_sha": bump_sha,
        "commit_date": "2026-01-02T00:00:00+00:00",
        "old_cap": 100,
        "new_cap": 200,
        "justification": _TEST_JUSTIFICATION,
    }

    justified_and_true = _phantom_ledger(repo, "m.py", dict(justified))
    # A justification cannot launder a phantom: a wrong old_cap still fails.
    justified_but_phantom = _phantom_ledger(
        repo, "m.py", {**justified, "old_cap": 50}
    )

    assert justified_and_true == []
    assert len(justified_but_phantom) == 1
