"""Unit tests for rollup_size_preflight.preflight_rollup_size_caps."""

import json
import httpx
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import rollup_size_preflight
from app.rollup_size_preflight import (
    _CAPS_JSON_PATH,
    preflight_rollup_size_caps as _preflight_rollup_size_caps,
)
from app.size_limit_policy import BumpRecord, CHRONIC_WINDOW_DAYS


_SAMPLE_CAPS = {
    "_doc": "test",
    "backend": {
        "models.py": 500,
        "rollup_management.py": 5550,
        "routers/admin_agent_ops.py": 4600,
        "release_qualifier.py": 10500,
    },
    "agent": {
        "handlers/strategic.ts": 1200,
    },
    "frontend": {
        "components/FleetRunnersPanel.tsx": 2000,
    },
    "testing": {"deleted_test.py": 500},
}

_CAPS_JSON = json.dumps(_SAMPLE_CAPS, indent=2) + "\n"
_PINNED_HEAD = "a" * 40

# The cap-bump-history ledger shipped alongside the caps file. The preflight
# fetches it at the pinned head before an auto-bump commit so the deployed
# backend's history mirror stays in sync; commit-path tests supply it as the
# third get_file_text_at_ref result.
_LEDGER = {"schema_version": 1, "history": {}}
_LEDGER_JSON = json.dumps(_LEDGER, indent=2) + "\n"


async def preflight_rollup_size_caps(*args, **kwargs):
    """Bind legacy fixtures to the immutable merge identity by default."""
    kwargs.setdefault("expected_head_sha", _PINNED_HEAD)
    kwargs.setdefault("target_branch", "automain")
    kwargs.setdefault("expected_base_sha", "b" * 40)
    return await _preflight_rollup_size_caps(*args, **kwargs)


@pytest.fixture(autouse=True)
def _stable_live_branch_head():
    """Keep existing preflight tests on one immutable synthetic branch tip."""
    with patch(
        "app.rollup_size_preflight.get_branch_sha",
        new_callable=AsyncMock,
        return_value=_PINNED_HEAD,
    ) as mock_head:
        yield mock_head


@pytest.fixture(autouse=True)
def _default_immutable_baseline():
    """Supply a clean synthetic base for tests that use legacy file mocks."""
    async def _archive(paths, *, commit_sha, **_kwargs):
        if commit_sha == _PINNED_HEAD:
            fetched = {}
            for path in paths:
                if path == _CAPS_JSON_PATH:
                    fetched[path] = _CAPS_JSON
                else:
                    fetched[path] = await rollup_size_preflight.get_file_text_at_ref(
                        path, ref=commit_sha, **_kwargs
                    )
            return fetched
        assert commit_sha == "b" * 40
        return {
            path: (_CAPS_JSON if path == _CAPS_JSON_PATH else "")
            for path in paths
        }

    with patch(
        "app.rollup_size_preflight.authoritative_texts_at_commit",
        new_callable=AsyncMock,
        side_effect=_archive,
    ):
        yield


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
async def test_small_rollup_verdict_is_reusable_at_the_merge_chokepoint():
    """A small-rollup verdict carries the exact target/head merge identity."""
    target_branch = "research/conflict-base"
    file_content = "line\n" * 400  # 400 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.authoritative_texts_at_commit", new_callable=AsyncMock, side_effect=_baseline_archive_texts(candidate=file_content, base=file_content)),
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content]

        result = await preflight_rollup_size_caps(
            "tok",
            "owner",
            "repo",
            42,
            "staging/branch-1",
            expected_head_sha=_PINNED_HEAD,
            target_branch=target_branch,
            expected_base_sha="b" * 40,
        )

    assert result.status == "ok"
    assert result.head_sha == _PINNED_HEAD
    assert result.target_branch == target_branch
    assert result.base_sha == "b" * 40
    assert result.is_bound_to(_PINNED_HEAD)
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_rejects_branch_head_mismatch_before_reading_tree():
    """A branch tip other than the intended merge head has no merge authority."""
    with patch(
        "app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock
    ) as mock_fetch:
        result = await preflight_rollup_size_caps(
            "tok",
            "owner",
            "repo",
            42,
            "staging/branch-1",
            expected_head_sha="b" * 40,
        )

    assert result.status == "defer"
    assert result.head_sha == _PINNED_HEAD
    assert "differs from the immutable merge candidate" in (result.reason or "")
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_without_immutable_base_defers():
    """An unbound call cannot silently fall back to absolute ratchet checks."""
    result = await _preflight_rollup_size_caps(
        "tok", "owner", "repo", 42, "staging/branch-1"
    )

    assert result.status == "defer"
    assert result.reason == "size-cap preflight lacks immutable merge-base SHA"


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
            _LEDGER_JSON,
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
async def test_large_rollup_growing_capped_file_without_bump_is_held():
    """A capped file grown by a large rollup cannot merge without its repair.

    The literal 500-line cap pin makes this a ratchet test: when the preflight
    cannot commit a cap repair (whether its bounded budget is spent or the
    file is chronic), a 501-line assembled tree must hold rather than silently
    authorizing the merge.
    """
    file_content = "line\n" * 501

    with (
        patch(
            "app.rollup_size_preflight.get_file_text_at_ref",
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            "app.rollup_size_preflight.get_pr_files",
            new_callable=AsyncMock,
        ) as mock_pr_files,
        patch(
            "app.rollup_size_preflight.commit_file_changes",
            new_callable=AsyncMock,
        ) as mock_commit,
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content]

        result = await preflight_rollup_size_caps(
            "tok",
            "owner",
            "repo",
            987,
            "staging/large-rollup",
            commit_bump=False,
            expected_head_sha=_PINNED_HEAD,
        )

    assert result.status == "hold"
    assert result.head_sha == _PINNED_HEAD
    assert result.is_bound_to(_PINNED_HEAD)
    assert "models.py" in (result.reason or "")
    mock_commit.assert_not_awaited()


def _baseline_archive_texts(*, candidate: str, base: str, base_caps: str = _CAPS_JSON):
    """Exact-SHA archive side effect for candidate/base preflight tests."""
    async def _read(paths, *, commit_sha, **_kwargs):
        assert "backend/app/models.py" in paths
        if commit_sha == _PINNED_HEAD:
            return dict.fromkeys(paths, candidate)
        assert commit_sha == "b" * 40
        return {
            path: (base_caps if path == _CAPS_JSON_PATH else base)
            for path in paths
        }
    return _read


@pytest.mark.asyncio
async def test_inherited_breach_is_reported_on_ok_verdict():
    """A touched over-cap file unchanged from the immutable base is reported, not repaired."""
    over_cap = "line\n" * 600
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock, return_value=_CAPS_JSON),
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock, return_value=_pr_files("backend/app/models.py")),
        patch("app.rollup_size_preflight.authoritative_texts_at_commit", new_callable=AsyncMock, side_effect=_baseline_archive_texts(candidate=over_cap, base=over_cap)) as archive_read,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as commit,
    ):
        result = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1",
            expected_head_sha=_PINNED_HEAD,
            target_branch="automain",
            expected_base_sha="b" * 40,
        )

    assert result.status == "ok"
    assert result.base_sha == "b" * 40
    assert result.reason == "inherited breach backend/app/models.py: 600 lines (cap 500)"
    assert archive_read.await_count == 2
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_inherited_breach_is_reported_alongside_new_breach():
    """An introduced breach must not erase inherited debt from the report."""
    candidate = {
        "backend/app/models.py": "line\n" * 600,
        "agent/src/handlers/strategic.ts": "line\n" * 1500,
    }
    baseline = {
        "backend/app/models.py": "line\n" * 600,
        "agent/src/handlers/strategic.ts": "line\n" * 1200,
    }

    async def _archive(paths, *, commit_sha, **_kwargs):
        if commit_sha == _PINNED_HEAD:
            return {path: candidate[path] for path in paths}
        return {
            path: (_CAPS_JSON if path == _CAPS_JSON_PATH else baseline[path])
            for path in paths
        }

    with (
        patch(
            "app.rollup_size_preflight.get_file_text_at_ref",
            new_callable=AsyncMock,
            side_effect=[_CAPS_JSON, _LEDGER_JSON],
        ),
        patch(
            "app.rollup_size_preflight.get_pr_files",
            new_callable=AsyncMock,
            return_value=_pr_files(
                "backend/app/models.py",
                "agent/src/handlers/strategic.ts",
            ),
        ),
        patch(
            "app.rollup_size_preflight.authoritative_texts_at_commit",
            new_callable=AsyncMock,
            side_effect=_archive,
        ),
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock),
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        result = await preflight_rollup_size_caps(
            "tok",
            "owner",
            "repo",
            42,
            "staging/branch-1",
            expected_head_sha=_PINNED_HEAD,
            target_branch="automain",
            expected_base_sha="b" * 40,
        )

    assert result.status == "bumped"
    assert "strategic.ts" in (result.reason or "")
    assert "inherited breach backend/app/models.py: 600 lines (cap 500)" in (
        result.reason or ""
    )


@pytest.mark.asyncio
async def test_worsened_inherited_breach_still_holds_without_repair_budget():
    """Growing an over-cap file beyond its over-cap base remains attributable."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock, return_value=_CAPS_JSON),
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock, return_value=_pr_files("backend/app/models.py")),
        patch("app.rollup_size_preflight.authoritative_texts_at_commit", new_callable=AsyncMock, side_effect=_baseline_archive_texts(candidate="line\n" * 601, base="line\n" * 600)),
    ):
        result = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1", commit_bump=False,
            expected_head_sha=_PINNED_HEAD, target_branch="automain", expected_base_sha="b" * 40,
        )

    assert result.status == "hold"
    assert "models.py" in (result.reason or "")


@pytest.mark.asyncio
async def test_new_breach_over_a_clean_base_still_holds_without_repair_budget():
    """A candidate crossing a cap cannot claim the base's clean size as inherited."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock, return_value=_CAPS_JSON),
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock, return_value=_pr_files("backend/app/models.py")),
        patch("app.rollup_size_preflight.authoritative_texts_at_commit", new_callable=AsyncMock, side_effect=_baseline_archive_texts(candidate="line\n" * 501, base="line\n" * 500)),
    ):
        result = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1", commit_bump=False,
            expected_head_sha=_PINNED_HEAD, target_branch="automain", expected_base_sha="b" * 40,
        )

    assert result.status == "hold"
    assert "models.py" in (result.reason or "")


@pytest.mark.asyncio
async def test_newly_added_over_cap_file_has_no_baseline_and_still_holds():
    """A known missing base path is new debt, not unavailable baseline evidence."""
    async def _candidate_and_missing_base(paths, *, commit_sha, **_kwargs):
        if commit_sha == _PINNED_HEAD:
            return dict.fromkeys(paths, "line\n" * 501)
        return {_CAPS_JSON_PATH: _CAPS_JSON}

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock, return_value=_CAPS_JSON),
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock, return_value=[{"filename": "backend/app/models.py", "status": "added"}]),
        patch("app.rollup_size_preflight.authoritative_texts_at_commit", new_callable=AsyncMock, side_effect=_candidate_and_missing_base),
    ):
        result = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1", commit_bump=False,
            expected_head_sha=_PINNED_HEAD, target_branch="automain", expected_base_sha="b" * 40,
        )

    assert result.status == "hold"
    assert "models.py" in (result.reason or "")


@pytest.mark.asyncio
async def test_candidate_cap_reduction_cannot_launder_a_clean_base_as_inherited():
    """Baseline attribution uses the base tree's cap, not the candidate's edit."""
    base_caps = json.loads(_CAPS_JSON)
    base_caps["backend"]["models.py"] = 700
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock, return_value=_CAPS_JSON),
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock, return_value=_pr_files("backend/app/models.py")),
        patch("app.rollup_size_preflight.authoritative_texts_at_commit", new_callable=AsyncMock, side_effect=_baseline_archive_texts(candidate="line\n" * 600, base="line\n" * 600, base_caps=json.dumps(base_caps))),
    ):
        result = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1", commit_bump=False,
            expected_head_sha=_PINNED_HEAD, target_branch="automain", expected_base_sha="b" * 40,
        )

    assert result.status == "hold"
    assert "models.py" in (result.reason or "")


@pytest.mark.asyncio
async def test_preflight_governed_breach_updates_json_source_and_ledger_together():
    """A governed repair commits all three artifacts at the pinned SHA.

    The repaired cap clamps to the module's declared governance constant rather
    than taking generic headroom, and the constant is committed unchanged: a
    governed module is never granted a larger ceiling by the self-heal.
    """
    qualifier_content = "line\n" * 10400
    qualifier_source = "_MODULE_SIZE_GOVERNANCE_CAP: int = 10900\n"
    caps = json.loads(_CAPS_JSON)
    caps["backend"]["release_qualifier.py"] = 10300
    caps_json = json.dumps(caps, indent=2) + "\n"

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/release_qualifier.py")
        mock_fetch.side_effect = [
            caps_json,
            qualifier_content,
            qualifier_source,
            _LEDGER_JSON,
        ]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    assert "10300 -> 10900" in reason
    upserts = mock_commit.call_args.kwargs["upserts"]
    repaired_caps = json.loads(upserts[_CAPS_JSON_PATH])
    assert repaired_caps["backend"]["release_qualifier.py"] == 10900
    assert "_MODULE_SIZE_GOVERNANCE_CAP: int = 10900" in upserts[
        "backend/app/release_qualifier.py"
    ]
    repaired_ledger = json.loads(upserts["backend/testing/cap_bump_history.json"])
    record = repaired_ledger["history"]["backend"]["release_qualifier.py"][-1]
    assert (record["old_cap"], record["new_cap"]) == (10300, 10900)


@pytest.mark.asyncio
async def test_preflight_prunes_deleted_testing_cap_and_ledger_history():
    """Deleted capped testing files are repaired through the preflight commit."""
    ledger = {
        "schema_version": 1,
        "history": {
            "testing": {
                "deleted_test.py": [
                    {
                        "commit_sha": "unattributed",
                        "commit_date": "2026-01-01T00:00:00+00:00",
                        "old_cap": 400,
                        "new_cap": 500,
                    }
                ]
            }
        },
    }
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_pr_files.return_value = [
            {"filename": "backend/testing/deleted_test.py", "status": "removed"}
        ]
        mock_fetch.side_effect = [_CAPS_JSON, json.dumps(ledger)]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    assert "remove testing/deleted_test.py" in reason
    upserts = mock_commit.call_args.kwargs["upserts"]
    repaired_caps = json.loads(upserts[_CAPS_JSON_PATH])
    assert "deleted_test.py" not in repaired_caps["testing"]
    repaired_ledger = json.loads(upserts["backend/testing/cap_bump_history.json"])
    assert "deleted_test.py" not in repaired_ledger["history"].get("testing", {})


@pytest.mark.asyncio
async def test_preflight_genuine_commit_failure_returns_hold():
    """A genuine (non-retryable) commit failure → hold verdict.

    A GitHub contract error like HTTP 400 is not a rate limit, not a
    non-fast-forward, and not a transport blip — it will not clear on retry, so
    the preflight must escalate (hold) rather than loop forever on defer.
    """
    file_content = "line\n" * 600

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]
        mock_commit.side_effect = RuntimeError(
            "GitHub Git Data API create-blob on owner/repo@staging "
            "returned HTTP 400 (expected 201)"
        )

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "hold"
    assert reason is not None
    assert "400" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_error",
    [
        # Non-fast-forward: the staging tip moved between read and PATCH.
        RuntimeError(
            "GitHub Git Data API update-ref on owner/repo@staging "
            "returned HTTP 422 (expected 200)"
        ),
        # Primary rate limit (5000/hr bucket exhausted).
        RuntimeError(
            "GitHub Git Data API create-blob on owner/repo@staging "
            "returned HTTP 403 (expected 201)"
        ),
        # Secondary/abuse rate limit.
        RuntimeError(
            "GitHub Git Data API create-tree on owner/repo@staging "
            "returned HTTP 429 (expected 201)"
        ),
        # GitHub server-side error.
        RuntimeError(
            "GitHub Git Data API create-commit on owner/repo@staging "
            "returned HTTP 502 (expected 201)"
        ),
        # Transport-layer/network failure re-raised by _github_request.
        httpx.ConnectError("connection reset by peer"),
    ],
)
async def test_preflight_transient_commit_failure_returns_defer(commit_error):
    """A transient/retryable commit failure → defer (retry next cycle), not hold."""
    file_content = "line\n" * 600

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]
        mock_commit.side_effect = commit_error

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert reason is not None
    mock_commit.assert_awaited_once()


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
async def test_preflight_caps_json_genuinely_absent_returns_ok():
    """Repo has no module_size_limits.json at all (unambiguous 404) → ok.

    A managed project repo not shaped like Blueprint simply has no size
    governance file; the preflight must let the rollup proceed rather than
    livelock it on defer every cycle.
    """
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock),
        patch("app.rollup_size_preflight.get_file_content_at_ref", new_callable=AsyncMock) as mock_exists,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        mock_fetch.return_value = None  # text fetch cannot read (404 or transient)
        mock_exists.return_value = (False, None)  # tri-state: unambiguous 404

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "ok"
    assert reason is None
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_caps_json_unreadable_returns_defer():
    """Caps JSON read is indeterminate (transient, not a definite 404) → defer.

    A transient GitHub blip must never be mistaken for 'no governance file' —
    otherwise a real breach could slip through unchecked.
    """
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock),
        patch("app.rollup_size_preflight.get_file_content_at_ref", new_callable=AsyncMock) as mock_exists,
    ):
        mock_fetch.return_value = None  # text fetch cannot read
        mock_exists.return_value = (None, None)  # tri-state: indeterminate/transient

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
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]

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
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]

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
async def test_preflight_removed_file_prunes_cap_and_returns_bumped():
    """A deleted capped file is committed out of the static guard artifacts."""
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
    ):
        # models.py is capped but marked removed in the PR.
        mock_pr_files.return_value = [{"filename": "backend/app/models.py", "status": "removed"}]
        mock_fetch.side_effect = [_CAPS_JSON, _LEDGER_JSON]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    assert "remove backend/models.py" in reason
    repaired = json.loads(mock_commit.call_args.kwargs["upserts"][_CAPS_JSON_PATH])
    assert "models.py" not in repaired["backend"]
    # The ledger joins the same commit, but deleted source content is never fetched.
    assert mock_fetch.await_count == 2
    assert all(
        call.args[0] != "backend/app/models.py" for call in mock_fetch.await_args_list
    )


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
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]

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
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]

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
async def test_preflight_auto_bump_records_ledger_bump():
    """An auto-bump commit must also record the bump in the cap-bump-history ledger.

    The deployed backend reads the ledger (not git), so a caps bump that is not
    mirrored into the ledger would leave production classifying from a stale
    history. The ledger must be fetched at the same pinned head and committed in
    the same commit as the caps change.
    """
    file_content = "line\n" * 600  # 600 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]

        verdict, _reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    mock_commit.assert_awaited_once()
    upserts = mock_commit.call_args.kwargs["upserts"]
    from app.rollup_size_preflight import _CAP_BUMP_HISTORY_LEDGER_RELPATH

    assert _CAP_BUMP_HISTORY_LEDGER_RELPATH in upserts
    ledger = json.loads(upserts[_CAP_BUMP_HISTORY_LEDGER_RELPATH])
    assert ledger["history"]["backend"]["models.py"][-1]["old_cap"] == 500
    assert ledger["history"]["backend"]["models.py"][-1]["new_cap"] == 800


@pytest.mark.asyncio
async def test_preflight_ledger_transient_read_failure_defers():
    """Ledger read is indeterminate (transient, not a definite 404) → defer.

    Committing a caps bump without mirroring it into the cap-bump-history
    ledger would leave the deployed backend classifying from a stale history,
    so a transient ledger read must defer the whole bump — mirroring the
    caps-file handling (a definite 404 skips the write, an indeterminate read
    defers).
    """
    file_content = "line\n" * 600  # 600 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.get_file_content_at_ref", new_callable=AsyncMock) as mock_exists,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        # caps JSON, models.py content, then ledger text fetch returns None.
        mock_fetch.side_effect = [_CAPS_JSON, file_content, None]
        mock_exists.return_value = (None, None)  # tri-state: indeterminate/transient

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert reason is not None
    assert "ledger" in reason
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_ledger_genuinely_absent_skips_ledger_write():
    """A staging branch with no ledger yet (definite 404) skips the ledger write.

    The pre-feature baseline must still auto-bump the caps file — a repo that
    has not shipped the ledger feature must not be livelocked on defer — but
    the commit must not fabricate a ledger either.
    """
    file_content = "line\n" * 600  # 600 lines, cap 500

    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.get_file_content_at_ref", new_callable=AsyncMock) as mock_exists,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        # caps JSON, models.py content, then ledger text fetch returns None.
        mock_fetch.side_effect = [_CAPS_JSON, file_content, None]
        mock_exists.return_value = (False, None)  # tri-state: unambiguous 404

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    assert reason is not None
    mock_commit.assert_awaited_once()
    from app.rollup_size_preflight import _CAP_BUMP_HISTORY_LEDGER_RELPATH

    upserts = mock_commit.call_args.kwargs["upserts"]
    assert _CAP_BUMP_HISTORY_LEDGER_RELPATH not in upserts


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
            side_effect=lambda key, section="backend", repo_root=None: (
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


@pytest.mark.asyncio
async def test_preflight_pins_reads_and_commit_to_same_head(_stable_live_branch_head):
    """Mutable branch names never feed the bump; one immutable SHA is leased."""
    file_content = "line\n" * 600
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]
        mock_commit.return_value = "b" * 40

        verdict, _ = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "bumped"
    assert _stable_live_branch_head.await_count == 2
    assert all(call.kwargs["ref"] == _PINNED_HEAD for call in mock_fetch.await_args_list)
    assert mock_commit.await_args.kwargs["expected_head_sha"] == _PINNED_HEAD


@pytest.mark.asyncio
async def test_preflight_concurrent_branch_advance_defers_without_commit(
    _stable_live_branch_head,
):
    """A concurrent branch update invalidates the read lease before any write."""
    _stable_live_branch_head.side_effect = [_PINNED_HEAD, "b" * 40]
    file_content = "line\n" * 600
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "defer"
    assert "advanced during" in (reason or "")
    mock_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_idempotent_commit_result_does_not_rearm_bump(
    _stable_live_branch_head,
):
    """Core no-diff result is treated as already current, not a fresh bump."""
    file_content = "line\n" * 600
    with (
        patch("app.rollup_size_preflight.get_file_text_at_ref", new_callable=AsyncMock) as mock_fetch,
        patch("app.rollup_size_preflight.get_pr_files", new_callable=AsyncMock) as mock_pr_files,
        patch("app.rollup_size_preflight.commit_file_changes", new_callable=AsyncMock) as mock_commit,
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
    ):
        mock_pr_files.return_value = _pr_files("backend/app/models.py")
        mock_fetch.side_effect = [_CAPS_JSON, file_content, _LEDGER_JSON]
        mock_commit.return_value = _PINNED_HEAD

        verdict, reason = await preflight_rollup_size_caps(
            "tok", "owner", "repo", 42, "staging/branch-1"
        )

    assert verdict == "ok"
    assert reason is None

    mock_commit.assert_awaited_once()
    mock_commit.assert_awaited_once()
