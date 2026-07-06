"""Unit tests for rollup_size_preflight.preflight_rollup_size_caps."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.rollup_size_preflight import preflight_rollup_size_caps, _CAPS_JSON_PATH
from app.size_limit_policy import BumpRecord, CHRONIC_WINDOW_DAYS


_SAMPLE_CAPS = {
    "_doc": "test",
    "backend": {
        "models.py": 500,
        "rollup_management.py": 5550,
        "routers/admin_agent_ops.py": 4600,
    },
    "agent": {
        "handlers/strategic.ts": 1200,
    },
    "frontend": {
        "components/FleetRunnersPanel.tsx": 2000,
    },
}

_CAPS_JSON = json.dumps(_SAMPLE_CAPS, indent=2) + "\n"


def _pr_files(*paths):
    return [{"filename": p, "status": "modified"} for p in paths]


@pytest.mark.asyncio
async def test_preflight_no_breach_returns_ok():
    """PR file within its cap → ok verdict, no commit."""
    file_content = "line\n" * 400  # 400 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [
            _CAPS_JSON,       # caps JSON fetch
            file_content,     # models.py fetch
        ]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "ok"
    assert reason is None
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_breach_auto_bumps_and_returns_bumped():
    """PR file over its cap → auto-bump committed, verdict bumped."""
    file_content = "line\n" * 600  # 600 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [
            _CAPS_JSON,
            file_content,
        ]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    assert reason is not None
    assert "models.py" in reason
    assert "500" in reason  # old cap mentioned

    mock_commit.assert_awaited_once()
    call_kwargs = mock_commit.call_args
    assert _CAPS_JSON_PATH in call_kwargs.kwargs["upserts"]
    updated = json.loads(call_kwargs.kwargs["upserts"][_CAPS_JSON_PATH])
    assert updated["backend"]["models.py"] > 500


@pytest.mark.asyncio
async def test_preflight_commit_failure_returns_hold():
    """Commit raises → hold verdict."""
    file_content = "line\n" * 600

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content]
        mock_commit.side_effect = RuntimeError("GitHub 422")

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "hold"
    assert reason is not None
    assert "422" in reason


@pytest.mark.asyncio
async def test_preflight_fetch_failure_returns_defer():
    """get_file_text_at_ref raises for the caps JSON → defer verdict."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock),
    ):
        mock_fetch.side_effect = Exception("connection refused")

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert reason is not None
    assert "module_size_limits.json" in reason


@pytest.mark.asyncio
async def test_preflight_pr_files_fetch_failure_returns_defer():
    """get_pr_files raises → defer verdict."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
    ):
        mock_fetch.return_value = _CAPS_JSON
        mock_pr_files.side_effect = RuntimeError("GitHub rate limit")

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert reason is not None


@pytest.mark.asyncio
async def test_preflight_uncapped_file_ignored():
    """A changed file with no cap entry is never treated as a breach."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files(
            "backend/app/uncapped_new_module.py"  # not in _SAMPLE_CAPS
        )
        mock_fetch.return_value = _CAPS_JSON  # only the caps JSON is fetched

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "ok"
    assert reason is None
    mock_commit.assert_not_called()
    # File content was never fetched (no cap entry to check against)
    assert mock_fetch.await_count == 1


@pytest.mark.asyncio
async def test_preflight_caps_json_missing_returns_defer():
    """Caps JSON not found at ref (returns None) → defer."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock),
    ):
        mock_fetch.return_value = None  # file not found

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert reason is not None


@pytest.mark.asyncio
async def test_preflight_agent_section_file_checked():
    """Files in the agent section (agent/src/) are also checked."""
    file_content = "line\n" * 1500  # over cap of 1200

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("agent/src/handlers/strategic.ts")
        mock_fetch.side_effect = [_CAPS_JSON, file_content]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 99, "staging/branch-2"
        )

    assert verdict == "bumped"
    assert "strategic.ts" in (reason or "")
    mock_commit.assert_awaited_once()
    updated = json.loads(mock_commit.call_args.kwargs["upserts"][_CAPS_JSON_PATH])
    assert updated["agent"]["handlers/strategic.ts"] > 1200


@pytest.mark.asyncio
async def test_preflight_commit_bump_false_returns_hold_budget_exhausted():
    """commit_bump=False with a breach → hold with 'budget exhausted' reason (no commit)."""
    file_content = "line\n" * 600  # 600 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1", commit_bump=False
        )

    assert verdict == "hold"
    assert reason is not None
    assert "budget exhausted" in reason
    assert "models.py" in reason
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_per_file_fetch_raises_returns_defer():
    """Per-file content fetch raises → defer (fail-safe, not skip)."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        # First call returns caps JSON; second (per-file fetch) raises.
        mock_fetch.side_effect = [_CAPS_JSON, Exception("connection reset")]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert reason is not None
    assert "models.py" in reason
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_per_file_fetch_returns_none_defers():
    """Per-file content fetch returns None for a non-deleted file → defer (not skip)."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        # First call returns caps JSON; second (per-file fetch) returns None.
        mock_fetch.side_effect = [_CAPS_JSON, None]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert reason is not None
    assert "models.py" in reason
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_removed_file_skipped_returns_ok():
    """A capped file with status 'removed' is intentionally skipped — no defer or breach."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        # models.py is capped but marked removed in the PR.
        mock_pr_files.return_value = [{"filename": "backend/app/models.py", "status": "removed"}]
        mock_fetch.return_value = _CAPS_JSON  # only caps JSON is fetched

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "ok"
    assert reason is None
    mock_commit.assert_not_called()
    # Only the caps JSON was fetched — per-file content was never requested.
    assert mock_fetch.await_count == 1


def _chronic_history() -> list[BumpRecord]:
    """Three recent bumps that meet the chronic-offender threshold."""
    now = datetime.now(timezone.utc)
    return [
        BumpRecord(
            commit_sha=f"sha{i}",
            commit_date=(now - timedelta(days=CHRONIC_WINDOW_DAYS - 10 * (i + 1))).isoformat(),
            old_cap=1000 + i * 200,
            new_cap=1200 + i * 200,
        )
        for i in range(3)
    ]


@pytest.mark.asyncio
async def test_chronic_offender_holds_not_bumped():
    """Acceptance criterion: a breach on a chronic offender returns ('hold', reason
    naming the file) with NO commit performed.

    This is the acceptance test named by the task's proof selector:
    test_rollup_size_preflight.py::test_chronic_offender_holds_not_bumped
    """
    file_content = "line\n" * 600  # 600 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=_chronic_history()),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "hold"
    assert reason is not None
    assert "models.py" in reason
    assert "decomposition" in reason.lower()
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_routine_breach_uses_classify_breach_new_limit():
    """Non-chronic breach commits the classify_breach suggested_new_limit, not a raw bump."""
    file_content = "line\n" * 600  # 600 lines, cap 500; classify_breach returns 900 for routine

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content]

        verdict, _reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    mock_commit.assert_awaited_once()
    updated = json.loads(mock_commit.call_args.kwargs["upserts"][_CAPS_JSON_PATH])
    # The committed cap must be the classify_breach suggested_new_limit for 600 lines:
    # bump_size_limit(600) = max(600+200, int(600*1.15)) rounded up to 50 = max(800, 690) -> 800
    assert updated["backend"]["models.py"] == 800


@pytest.mark.asyncio
async def test_preflight_mixed_chronic_and_routine_holds_without_commit():
    """A rollup with both a chronic and a routine breach must hold — no partial commit."""
    chronic_content = "line\n" * 600   # models.py: 600 lines, cap 500 — chronic
    routine_content = "line\n" * 5700  # rollup_management.py: 5700 lines, cap 5550 — routine

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch(
            "app.size_limit_policy.parse_bump_history",
            side_effect=lambda key, section="backend": (
                _chronic_history() if key == "models.py" else []
            ),
        ),
    ):
        mock_pr_files.return_value = _pr_files(
            "backend/app/models.py",
            "backend/app/rollup_management.py",
        )
        mock_fetch.side_effect = [_CAPS_JSON, chronic_content, routine_content]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "hold"
    assert reason is not None
    assert "models.py" in reason
    assert "decomposition" in reason.lower()
    mock_commit.assert_not_called()
