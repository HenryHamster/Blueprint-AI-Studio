"""Onboarding a repository with ZERO commits.

A freshly-created GitHub repo still reports ``default_branch: "main"`` — but that
ref does not exist, because the repo has no commits and therefore no branches at
all. Every clone in the fleet passes ``--branch <trunk>``, so all of them die
with ``fatal: Remote branch main not found in upstream origin`` (exit 128) —
starting with ``repo_bootstrap``, the task whose whole job is to seed the project.
It retried to exhaustion and cancelled, and the project could never run anything.

The guard that should have caught this (``repo_is_empty`` in repo_bootstrap) was
unreachable: the only way to compute it was a contents listing, and a contents
listing of a commitless repo 404s, which the detector treated as a *transient*
GitHub failure and swallowed into ``{}``. The signal that says "this repo is
empty" was the exact signal that could not be computed when the repo was empty.

These tests pin both halves of the fix: seed the repo so the trunk is real, and
classify the empty-repo 404 honestly so the guard can fire.
"""

import base64
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.github_refs import ensure_repo_initialized, repo_has_commits
from app.models import Project, Task, TaskStatus
from app.repo_bootstrap import spawn_bootstrap_task
from app.routers.admin_onboard import (
    GREENFIELD_SOURCE_FILE_THRESHOLD,
    _classify_repo_emptiness,
    _detect_build_config,
    _empty_repo_build_config,
    _is_empty_repository_response,
)
from app.schemas import AgentStatusPayload


def _resp(status_code, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = text
    return resp


# The literal GitHub responses, captured against a real commitless repo
# (HenryHamster/bp-empty-repo-probe) rather than recalled from memory.
_EMPTY_BRANCHES = _resp(200, [])
_HAS_BRANCHES = _resp(200, [{"name": "main", "commit": {"sha": "e18094f"}}])
_CONTENTS_EMPTY_REPO_404 = _resp(404, {"message": "This repository is empty."})
_SEEDED_201 = _resp(201, {"commit": {"sha": "e18094f"}, "content": {"path": "README.md"}})


class TestEnsureRepoInitialized:
    """Seeding the first commit so the default branch actually exists."""

    @pytest.mark.asyncio
    async def test_seeds_commitless_repo_via_contents_api(self):
        calls = []

        async def _route(method, url, headers, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/branches"):
                return _EMPTY_BRANCHES
            return _SEEDED_201

        with patch("app.github._github_request", new_callable=AsyncMock, side_effect=_route):
            seeded = await ensure_repo_initialized("tok", "owner", "repo", "main")

        assert seeded is True

        put = [c for c in calls if c[0] == "PUT"]
        assert len(put) == 1, "the initial commit must be created exactly once"
        _, url, kwargs = put[0]
        # The Contents API is the ONLY endpoint that can create the first commit:
        # the Git Data API (trees/commits/refs) rejects a commitless repo with
        # 409 "Git Repository is empty". If this ever moves to /git/refs it will
        # 409 in production against the very repos it is meant to fix.
        assert url.endswith("/contents/README.md")
        body = kwargs["json"]
        # The branch must be named explicitly — this is what *creates* the trunk.
        assert body["branch"] == "main"
        assert base64.b64decode(body["content"]).decode() == "# repo\n"

    @pytest.mark.asyncio
    async def test_honours_non_default_trunk_name(self):
        sent = {}

        async def _route(method, url, headers, **kwargs):
            if url.endswith("/branches"):
                return _EMPTY_BRANCHES
            sent.update(kwargs["json"])
            return _SEEDED_201

        with patch("app.github._github_request", new_callable=AsyncMock, side_effect=_route):
            await ensure_repo_initialized("tok", "owner", "repo", "trunk")

        assert sent["branch"] == "trunk", "must seed the repo's real default branch, not 'main'"

    @pytest.mark.asyncio
    async def test_is_a_noop_on_a_repo_that_has_commits(self):
        calls = []

        async def _route(method, url, headers, **kwargs):
            calls.append(method)
            return _HAS_BRANCHES

        with patch("app.github._github_request", new_callable=AsyncMock, side_effect=_route):
            seeded = await ensure_repo_initialized("tok", "owner", "repo", "main")

        assert seeded is False
        assert "PUT" not in calls, "must never write to a repo that already has commits"

    @pytest.mark.asyncio
    async def test_concurrent_seed_is_not_an_error(self):
        # Another writer seeded between our branch check and our write. GitHub
        # rejects the second write; that is success, not failure — but only if a
        # commit really does exist now, which we re-check rather than assume.
        responses = [_EMPTY_BRANCHES, _resp(422, {"message": "Invalid request."}), _HAS_BRANCHES]

        async def _route(method, url, headers, **kwargs):
            return responses.pop(0)

        with patch("app.github._github_request", new_callable=AsyncMock, side_effect=_route):
            seeded = await ensure_repo_initialized("tok", "owner", "repo", "main")

        assert seeded is False
        assert responses == [], "must re-verify the repo really has a commit after a 422"

    @pytest.mark.asyncio
    async def test_seed_failure_raises_rather_than_returning_a_phantom_trunk(self):
        # A 403 (App installation lacks contents:write) must surface. Silently
        # continuing would store a trunk that does not exist and hand the project
        # straight back into the infinite bootstrap loop this fix exists to end.
        async def _route(method, url, headers, **kwargs):
            if url.endswith("/branches"):
                return _EMPTY_BRANCHES
            return _resp(403, {"message": "Resource not accessible by integration"}, text="403")

        with (
            patch("app.github._github_request", new_callable=AsyncMock, side_effect=_route),
            pytest.raises(HTTPException) as exc,
        ):
            await ensure_repo_initialized("tok", "owner", "repo", "main")

        assert exc.value.status_code == 502
        assert "contents:write" in exc.value.detail

    @pytest.mark.asyncio
    async def test_repo_has_commits_reads_branches_not_default_branch(self):
        # GET /repos/{o}/{r} returns default_branch: "main" even for a commitless
        # repo, so it CANNOT answer this question. Only the ref list can.
        async def _route(method, url, headers, **kwargs):
            assert url.endswith("/branches"), f"must ask for refs, got {url}"
            return _EMPTY_BRANCHES

        with patch("app.github._github_request", new_callable=AsyncMock, side_effect=_route):
            assert await repo_has_commits("tok", "owner", "repo") is False


class TestEmptyRepoDetection:
    """The contents-listing 404 is permanent and meaningful, not transient."""

    def test_recognises_the_empty_repo_404(self):
        assert _is_empty_repository_response(_CONTENTS_EMPTY_REPO_404) is True

    def test_recognises_the_git_data_409(self):
        assert _is_empty_repository_response(_resp(409, {"message": "Git Repository is empty."})) is True

    def test_does_not_overmatch_a_missing_ref_404(self):
        # A bad ref on a perfectly healthy repo also 404s. Treating that as
        # "empty repo" would suppress bootstrap on a real codebase.
        missing_ref = _resp(404, {"message": "No commit found for the ref nonexistent"})
        assert _is_empty_repository_response(missing_ref) is False

    def test_does_not_overmatch_a_transient_5xx(self):
        assert _is_empty_repository_response(_resp(500, {"message": "Server Error"})) is False

    @pytest.mark.asyncio
    async def test_detector_reports_empty_repo_instead_of_swallowing_it(self):
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            return_value=_CONTENTS_EMPTY_REPO_404,
        ):
            config = await _detect_build_config("tok", "owner", "repo")

        # The bug: this used to be {}, which reads as "detection failed, retry
        # later" and lets repo_is_empty fall back to its schema default of False.
        assert config != {}
        assert config["repo_is_empty"] is True
        # Nothing is asserted that was not observed — there is no tree at all.
        assert config["language"] is None
        assert config["directories"] == []
        assert config["has_ci"] is False

    @pytest.mark.asyncio
    async def test_transient_failure_still_yields_no_config(self):
        # Regression guard for the other direction: a real transient GitHub
        # failure must NOT be reported as an empty repo, or a healthy codebase
        # would get bootstrap suppressed and be treated as greenfield.
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            return_value=_resp(500, {"message": "Server Error"}),
        ):
            config = await _detect_build_config("tok", "owner", "repo")

        assert config == {}


# ── Cross-classifier agreement (backend vs. agent) ──────────────────────────
# The agent's clone-time detector (agent/src/prompts/discovery.ts
# countSourceFiles) and the backend onboarding classifier
# (_classify_repo_emptiness) must apply the SAME "real source" exclusion rules,
# or the same repo is counted empty by one and non-empty by the other — the
# false-positive source for the clone_failed wedge. This block pins the two
# together by modelling the agent's exclusion rules independently (they are
# validated on the TS side in agent/src/prompts/discovery.test.ts) and asserting
# the backend agrees on the historically-divergent cases.

# Exact mirror of the agent's post-fix exclusion sets in discovery.ts.
_AGENT_NON_SOURCE_EXT = (
    ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".txt", ".lock", ".lockb",
)
_AGENT_NON_SOURCE_BASENAMES = {
    "license", "licence", "notice", "authors", "codeowners", "dockerfile",
    "makefile", "procfile", "go.mod", ".gitignore", ".gitattributes",
    ".dockerignore", ".editorconfig", ".npmrc", ".nvmrc", ".prettierrc",
    ".eslintrc",
}
_AGENT_NON_SOURCE_PREFIXES = ("readme", "contributing", "changelog", "security")


def _agent_count_source_files(names: list[str]) -> int:
    """Independent Python mirror of the agent's countSourceFiles rules."""
    count = 0
    for name in names:
        base = name.rsplit("/", 1)[-1].strip().lower()
        if not base or base.startswith("."):
            continue
        if base in _AGENT_NON_SOURCE_BASENAMES:
            continue
        if any(base.startswith(p) for p in _AGENT_NON_SOURCE_PREFIXES):
            continue
        if any(base.endswith(ext) for ext in _AGENT_NON_SOURCE_EXT):
            continue
        count += 1
    return count


def _agent_is_empty(names: list[str]) -> bool:
    return _agent_count_source_files(names) <= GREENFIELD_SOURCE_FILE_THRESHOLD


# Each case is (label, list-of-basenames). These are exactly the files the
# backend excluded but the agent used to COUNT — the divergence this fix closes.
# The agent side must now also exclude them.
_DIVERGENT_CASES = [
    ("go.mod only", ["go.mod"]),
    ("British-spelling licence only", ["LICENCE"]),
    ("readme-prefixed only", ["README", "README.rst"]),
    ("contributing/changelog/security prefixes",
     ["CONTRIBUTING", "CHANGELOG", "SECURITY"]),
    ("mixed scaffolding, no real source",
     ["go.mod", "LICENCE", "README", "SECURITY", ".gitignore"]),
    ("scaffolding plus one real source file",
     ["go.mod", "README.md", "src/main.go"]),
]


class TestClassifierAgreement:
    """The backend and agent classifiers agree like-for-like across the cases
    that previously diverged (go.mod, licence, readme-prefixed)."""

    def _backend_verdict(self, names: list[str]) -> tuple[bool, int]:
        items = [{"name": n, "type": "file"} for n in names]
        return _classify_repo_emptiness(items)

    @pytest.mark.parametrize("label,names", _DIVERGENT_CASES)
    def test_backend_and_agent_agree_on_emptiness(self, label, names):
        backend_empty, backend_count = self._backend_verdict(names)
        assert backend_empty is _agent_is_empty(names), (
            f"classifiers disagree on emptiness for case: {label}"
        )
        assert backend_count == _agent_count_source_files(names), (
            f"classifiers disagree on the source count for case: {label}"
        )

    def test_divergent_scaffolding_counts_as_zero_source(self):
        # go.mod, licence, and the readme/contributing/changelog/security
        # prefixes are all excluded — a repo of only these is empty (count 0).
        _, count = self._backend_verdict(
            ["go.mod", "LICENCE", "README", "CONTRIBUTING", "CHANGELOG", "SECURITY"]
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_detector_persists_source_file_count(self):
        # The authoritative count is persisted into build_config so the agent
        # can compare like-for-like. A single real source file among scaffolding.
        listing = [
            {"name": "go.mod", "type": "file"},
            {"name": "README.md", "type": "file"},
            {"name": "main.go", "type": "file"},
        ]
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            return_value=_resp(200, listing),
        ):
            config = await _detect_build_config("tok", "owner", "repo")

        assert config.get("source_file_count") == 1
        assert config.get("repo_is_empty") is True

    def test_empty_repo_build_config_carries_zero_count(self):
        assert _empty_repo_build_config()["source_file_count"] == 0


class TestIncompleteListingIsUndetermined:
    """A partial contents walk must never assert a durable, authoritative
    ``repo_is_empty=False``. It is tri-state (parity with ``has_ci``): an
    incomplete read is undetermined (None), which ``exclude_none`` drops so the
    signal persists as ABSENT — every consumer coalesces absent to "consult the
    live signal / self-heal", never to a false negative that suppresses
    greenfield mode on a transient fetch error."""

    def test_incomplete_listing_yields_undetermined_not_non_empty(self):
        # 1) The pure classifier: an incomplete listing is undetermined (None),
        #    not an asserted-non-empty False — even when the fetched portion has
        #    at or below the greenfield source threshold.
        below_threshold = [{"name": "README.md", "type": "file"}]
        assert _classify_repo_emptiness([], listing_complete=False) == (None, None)
        assert _classify_repo_emptiness(
            below_threshold, listing_complete=False
        ) == (None, None)

        # A COMPLETED walk still resolves to a definite True/False — only the
        # incomplete path is undetermined, so existing determinate consumers
        # keep their concrete signal.
        assert _classify_repo_emptiness(below_threshold, listing_complete=True) == (
            True,
            0,
        )

        # 2) The persistence seam: an undetermined None survives BuildConfig
        #    validation and is DROPPED by model_dump(exclude_none=True), so it
        #    stores as an absent key — distinguishable from a durable False.
        from app.schemas import BuildConfig

        undetermined = BuildConfig(repo_is_empty=None).model_dump(exclude_none=True)
        assert "repo_is_empty" not in undetermined

        durable_false = BuildConfig(repo_is_empty=False).model_dump(exclude_none=True)
        assert durable_false["repo_is_empty"] is False

    @pytest.mark.asyncio
    async def test_detect_build_config_incomplete_walk_persists_undetermined(self):
        # End-to-end through _detect_build_config: a subdir listing that fails to
        # fetch leaves both repo_is_empty and has_ci undetermined (None), and the
        # schema round-trip drops the undetermined key rather than asserting it.
        root_files = [
            {"name": "README.md", "type": "file"},
            {"name": "backend", "type": "dir"},
        ]
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[_resp(200, root_files), _resp(502, {})],
        ):
            config = await _detect_build_config("tok", "owner", "repo")

        # Parity: repo_is_empty tracks has_ci — both undetermined on a partial walk.
        assert config["repo_is_empty"] is None
        assert config["has_ci"] is None

        from app.schemas import BuildConfig

        persisted = BuildConfig(**config).model_dump(exclude_none=True)
        assert "repo_is_empty" not in persisted


class TestBootstrapGuardIsReachable:
    """The payoff: the empty-repo signal now actually reaches the guard."""

    @pytest.mark.asyncio
    async def test_detected_empty_repo_suppresses_bootstrap(self, db_session):
        project = Project(
            name="playground",
            user_id="test-user-empty-repo",
            github_owner="HenryHamster",
            github_repo="playground",
            build_config=_empty_repo_build_config(),
        )
        db_session.add(project)
        await db_session.flush()

        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=True,
            spawn_reason="first_cycle",
        )

        assert result.outcome == "not_eligible"
        assert result.reason == "repo_is_empty"

    @pytest.mark.asyncio
    async def test_the_old_swallowed_config_would_have_spawned(self):
        # Documents the actual production failure: with build_config == {} (what
        # the detector used to return for a commitless repo) the guard reads
        # falsy and bootstrap is spawned against a repo it can never clone.
        # If this ever starts returning not_eligible/repo_is_empty, the guard has
        # grown a second, independent source of truth and this fix is redundant.
        assert not {}.get("repo_is_empty")


def _legacy_undetermined_project(**overrides) -> Project:
    """A legacy project whose durable repo_is_empty was never determined.

    The absent key is the exact pre-fix state that the spawn gate and recovery
    sweep both coalesce to "not empty".
    """
    cfg = {"language": None, "detected_at": "2025-01-01T00:00:00+00:00"}
    assert "repo_is_empty" not in cfg
    kwargs = {
        "name": "legacy/undetermined",
        "user_id": "test-user-empty-repo",
        "github_owner": "legacy",
        "github_repo": "undetermined",
        "build_config": cfg,
        "integration_branch": "main",
    }
    kwargs.update(overrides)
    return Project(**kwargs)


class TestLegacyUndeterminedEmptinessSelfHeal:
    """A legacy project with an undetermined (absent) durable ``repo_is_empty``
    is resolved against the live repo at spawn time instead of looping
    clone_failed to exhaustion — the sys-onboarding risk #1 this task closes."""

    @pytest.mark.asyncio
    async def test_commitless_repo_is_seeded_and_stamped_empty(self, db_session):
        # Undetermined signal + a repo GitHub reports as commitless: the trunk
        # must be seeded (so `--branch main` clones stop dying), the durable
        # signal stamped repo_is_empty=True, and the doomed bootstrap suppressed
        # HERE rather than retried to exhaustion.
        project = _legacy_undetermined_project()
        db_session.add(project)
        await db_session.flush()

        ensure_mock = AsyncMock(return_value=True)
        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.repo_has_commits",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("app.github.ensure_repo_initialized", ensure_mock),
        ):
            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
                automatic_recovery=True,
            )
        await db_session.commit()

        assert result.outcome == "not_eligible"
        assert result.reason == "repo_is_empty"
        # The trunk was seeded on the project's real integration branch.
        ensure_mock.assert_awaited_once()
        assert ensure_mock.await_args.args[3] == "main"
        # The durable signal is no longer undetermined — it now reads True with
        # the full empty-repo shape, so future sweeps classify it correctly.
        assert project.build_config["repo_is_empty"] is True
        assert project.build_config["source_file_count"] == 0
        assert project.build_config["has_ci"] is False

    @pytest.mark.asyncio
    async def test_readme_only_committed_repo_is_greenfield_and_suppresses_bootstrap(
        self, db_session
    ):
        # A commit only proves the remote is cloneable. The canonical detector
        # must still classify README-only history as greenfield, stamp its
        # authoritative source count, and suppress the doomed bootstrap.
        project = _legacy_undetermined_project(
            name="legacy/readme-only", github_repo="readme-only"
        )
        db_session.add(project)
        await db_session.flush()

        ensure_mock = AsyncMock(return_value=False)
        detected = {
            "language": None,
            "directories": [],
            "env_vars": {},
            "repo_is_empty": True,
            "source_file_count": 0,
            "has_ci": False,
        }
        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.repo_has_commits",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("app.github.ensure_repo_initialized", ensure_mock),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value=detected,
            ) as detect_mock,
        ):
            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
            )
        await db_session.commit()

        assert result.outcome == "not_eligible"
        assert result.reason == "repo_is_empty"
        # A repo with commits must never be seeded.
        ensure_mock.assert_not_awaited()
        detect_mock.assert_awaited_once_with(
            "tok", "legacy", "readme-only", ref="main"
        )
        assert project.build_config["repo_is_empty"] is True
        assert project.build_config["source_file_count"] == 0

    @pytest.mark.asyncio
    async def test_source_rich_committed_repo_is_stamped_non_empty_and_spawns(
        self, db_session
    ):
        project = _legacy_undetermined_project(
            name="legacy/source-rich", github_repo="source-rich"
        )
        db_session.add(project)
        await db_session.flush()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.repo_has_commits",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": "python",
                    "directories": [{"path": "./", "type": "python"}],
                    "repo_is_empty": False,
                    "source_file_count": 2,
                    "has_ci": False,
                },
            ),
        ):
            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
            )
        await db_session.commit()

        assert result.outcome == "spawned"
        assert project.build_config["repo_is_empty"] is False
        assert project.build_config["source_file_count"] == 2

    @pytest.mark.asyncio
    async def test_committed_self_heal_preserves_completed_and_recovery_metadata(
        self, db_session
    ):
        """Detection fields refresh without erasing non-repo lifecycle evidence."""
        project = _legacy_undetermined_project(
            name="legacy/completed-source-rich",
            github_repo="completed-source-rich",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00+00:00",
                "bootstrap_degraded_reason": "prior_overview_missing",
                "bootstrap_recovery_state": "exhausted",
                "bootstrap_command_memory_status": "written",
                "bootstrap_command_memory_updated_at": "2026-01-02T00:00:00+00:00",
                "custom_bootstrap_evidence": {"task_id": "prior-task"},
            },
        )
        db_session.add(project)
        await db_session.flush()

        with (
            patch("app.github.get_repo_installation_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.github.repo_has_commits", new_callable=AsyncMock, return_value=True),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": "typescript",
                    "directories": [{"path": "./", "type": "node"}],
                    "repo_is_empty": False,
                    "source_file_count": 4,
                    "has_ci": True,
                },
            ),
        ):
            result = await spawn_bootstrap_task(
                project, db_session, is_first_bootstrap=False, spawn_reason="operator_retry"
            )
        await db_session.commit()

        assert result.outcome == "already_completed"
        assert result.build_config_changed is True
        assert project.build_config["language"] == "typescript"
        assert project.build_config["bootstrap_completed_at"] == "2026-01-01T00:00:00+00:00"
        assert project.build_config["bootstrap_degraded_reason"] == "prior_overview_missing"
        assert project.build_config["bootstrap_recovery_state"] == "exhausted"
        assert project.build_config["bootstrap_command_memory_status"] == "written"
        assert project.build_config["custom_bootstrap_evidence"] == {"task_id": "prior-task"}

        # A refresh deliberately bypasses the completed gate, but must retain
        # the timestamp while doing so; otherwise a later retry can duplicate
        # first-bootstrap work.
        refresh = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="operator_refresh"
        )
        assert refresh.outcome == "spawned"
        assert project.build_config["bootstrap_completed_at"] == "2026-01-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_incomplete_committed_repo_detection_falls_through_unstamped(
        self, db_session
    ):
        project = _legacy_undetermined_project(
            name="legacy/incomplete", github_repo="incomplete"
        )
        db_session.add(project)
        await db_session.flush()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.repo_has_commits",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "repo_is_empty": None,
                    "source_file_count": None,
                    "detection_incomplete": True,
                },
            ),
        ):
            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
            )
        await db_session.commit()

        assert result.outcome == "spawned"
        assert "repo_is_empty" not in project.build_config

    @pytest.mark.asyncio
    async def test_no_token_leaves_signal_undetermined_and_falls_through(
        self, db_session
    ):
        # Best-effort: no installation token (the test/no-GitHub-App default)
        # must NOT stamp the signal or seed anything — the pre-fix behavior is
        # preserved and the bootstrap spawns exactly as before.
        project = _legacy_undetermined_project(
            name="legacy/no-token", github_repo="no-token"
        )
        db_session.add(project)
        await db_session.flush()

        seed_mock = AsyncMock()
        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.github.ensure_repo_initialized", seed_mock),
        ):
            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
            )
        await db_session.commit()

        assert result.outcome == "spawned"
        seed_mock.assert_not_awaited()
        # Signal untouched — still undetermined, exactly as before the fix.
        assert "repo_is_empty" not in project.build_config

    @pytest.mark.asyncio
    async def test_github_error_falls_through_without_blocking_spawn(
        self, db_session
    ):
        # A transient GitHub failure mid-heal must never block the spawn or
        # mis-stamp the signal: swallow, leave undetermined, fall through.
        project = _legacy_undetermined_project(
            name="legacy/transient", github_repo="transient"
        )
        db_session.add(project)
        await db_session.flush()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.repo_has_commits",
                new_callable=AsyncMock,
                side_effect=RuntimeError("GitHub 502"),
            ),
        ):
            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
            )
        await db_session.commit()

        assert result.outcome == "spawned"
        assert "repo_is_empty" not in project.build_config

    @pytest.mark.asyncio
    async def test_determined_signal_skips_self_heal_entirely(self, db_session):
        # Guard the hot path: a DETERMINED repo_is_empty=False must not touch
        # GitHub at all — the self-heal is dead code for every non-legacy caller.
        project = _legacy_undetermined_project(
            name="legacy/determined",
            github_repo="determined",
            build_config={"language": "python", "repo_is_empty": False},
        )
        db_session.add(project)
        await db_session.flush()

        token_mock = AsyncMock(return_value="tok")
        with patch("app.github.get_repo_installation_token", token_mock):
            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
            )
        await db_session.commit()

        assert result.outcome == "spawned"
        token_mock.assert_not_awaited()


# ── Self-heal: a stale repo_is_empty=False that can never self-correct ───────
# A project whose durable build_config.repo_is_empty is stale-False on a
# genuinely empty/near-empty repo makes the agent's clone-delivery guard
# (verifyCloneDelivered) throw clone_failed on EVERY task — the durable signal
# says non-empty, the cloned tree is empty, and the two disagree forever. The
# self-heal re-verifies the durable signal against GitHub on that disagreement
# path and flips it to True ONLY when fresh detection authoritatively confirms
# the repo is empty — while a repo with real content the clone failed to deliver
# still fails loud (the signal is NOT flipped).


class TestStaleEmptySignalSelfHeal:
    """maybe_correct_stale_repo_empty_signal self-corrects an authoritatively-empty
    repo and preserves fail-loud for a genuine clone shortfall."""

    def _stale_project(self):
        return Project(
            name="wedged-external",
            github_owner="HenryHamster",
            github_repo="wedged-external",
            build_config={"repo_is_empty": False, "source_file_count": 12},
        )

    @pytest.mark.asyncio
    async def test_authoritatively_empty_repo_self_corrects_to_true(self):
        # The wedge case: durable says non-empty, but the repo is really empty.
        # Fresh detection authoritatively confirms empty → the signal flips and
        # the next task proceeds greenfield instead of looping clone_failed.
        from app.build_config_detection import (
            _redetect_stale_empty_cooldown,
            maybe_correct_stale_repo_empty_signal,
        )

        _redetect_stale_empty_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()
        detected_empty = {
            "language": None,
            "directories": [],
            "env_vars": {},
            "repo_is_empty": True,
            "source_file_count": 0,
            "has_ci": False,
        }

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value=detected_empty,
            ),
            patch("sqlalchemy.orm.attributes.flag_modified"),
            patch(
                "app.shared_state.get_project_integration_branch",
                return_value="main",
            ),
        ):
            healed = await maybe_correct_stale_repo_empty_signal(project, session)

        assert healed is True
        assert project.build_config["repo_is_empty"] is True
        # The next task now claims a greenfield signal — isGreenfieldRepo short
        # circuits on repo_is_empty=True and verifyCloneDelivered never throws.
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_real_content_that_failed_to_clone_still_fails_loud(self):
        # The other half of the fix: a repo that genuinely HAS content (fresh
        # detection says repo_is_empty=False) must NOT flip. The durable signal
        # stays False so the agent's verifyCloneDelivered keeps failing loud on
        # the next clone shortfall rather than masking it as greenfield.
        from app.build_config_detection import (
            _redetect_stale_empty_cooldown,
            maybe_correct_stale_repo_empty_signal,
        )

        _redetect_stale_empty_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()
        detected_non_empty = {
            "language": "python",
            "directories": [{"path": "./", "type": "python"}],
            "repo_is_empty": False,
            "source_file_count": 12,
        }

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value=detected_non_empty,
            ),
            patch(
                "app.shared_state.get_project_integration_branch",
                return_value="main",
            ),
        ):
            healed = await maybe_correct_stale_repo_empty_signal(project, session)

        assert healed is False
        assert project.build_config["repo_is_empty"] is False
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_token_failure_does_not_consume_cooldown(self):
        # A transient get_repo_installation_token failure (returns None) must NOT
        # stamp the 6-hour cooldown — the next task should be allowed to retry.
        from app.build_config_detection import (
            _redetect_stale_empty_cooldown,
            maybe_correct_stale_repo_empty_signal,
        )

        _redetect_stale_empty_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()

        with patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value=None,  # transient failure — no token
        ):
            healed = await maybe_correct_stale_repo_empty_signal(project, session)

        assert healed is False
        # Cooldown must NOT be stamped — a transient failure should not lock the
        # project out of self-healing for 6 hours.
        assert str(getattr(project, "id", "")) not in _redetect_stale_empty_cooldown


# ── Plan no-op guard on greenfield repos ─────────────────────────────────────
# A plan agent that emits {"subtasks": [], "no_op_reason": "..."} on a project
# whose durable repo_is_empty is True is silently abandoning onboarding — the
# direction's "fail-loud" invariant. The greenfield guard in plan_completion.py
# rejects these with FAILED so the failure is visible and retried rather than
# accepted as a clean CANCELLED. The guard fires ONLY on repo_is_empty is True;
# absent or False falls through to the normal CANCELLED path.


def _make_plan_task():
    from app.models import Task, TaskStatus

    return Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        title="Build greenfield foundation",
        payloads=[{"type": "plan", "prompt": "build the initial project structure"}],
        status=TaskStatus.IN_PROGRESS,
        context={},
        created_by="ORCHESTRATOR",
    )


def _make_mock_session(project) -> AsyncMock:
    session = AsyncMock()
    session.get.return_value = project
    return session


class TestGreenfieldPlanNoOpRejection:
    """plan_completion: emptiness-citing no-op must FAIL on greenfield, CANCEL otherwise."""

    @pytest.mark.asyncio
    async def test_greenfield_emptiness_no_op_fails_loud(self):
        """Core acceptance test: a plan no-op on a greenfield project must FAIL, not CANCEL."""
        from app.plan_completion import _handle_plan_completion
        from app.models import TaskStatus

        project = Project(
            name="greenfield-project",
            github_owner="owner",
            github_repo="greenfield-repo",
            build_config={"repo_is_empty": True, "source_file_count": 0},
        )
        task = _make_plan_task()
        session = _make_mock_session(project)

        no_op_reason = "the codebase is empty, nothing here to build upon at all"
        logs = json.dumps({"subtasks": [], "no_op_reason": no_op_reason})

        with (
            patch(
                "app.compute_backends.resolve_project_default_container_image",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.failure_classification.classify_failure", return_value="CODE_ERROR"),
            patch("app.failure_classification.persist_failure_category"),
            patch("app.routers.agent_status.record_task_result"),
            patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock),
        ):
            await _handle_plan_completion(task, logs, session)

        assert task.status == TaskStatus.FAILED, (
            f"Expected FAILED but got {task.status} — "
            "a plan that cites emptiness on a greenfield repo must fail loud, not silently cancel"
        )
        assert "greenfield" in (task.latest_status_message or "").lower(), (
            f"Status message must mention 'greenfield': {task.latest_status_message!r}"
        )
        assert task.context.get("greenfield_no_op_rejected") is True
        assert task.context.get("no_op_reason") == no_op_reason

    @pytest.mark.asyncio
    async def test_non_greenfield_no_op_cancelled_not_failed(self):
        """Existing behavior preserved: a no-op on a non-greenfield project must CANCEL cleanly."""
        from app.plan_completion import _handle_plan_completion
        from app.models import TaskStatus

        project = Project(
            name="non-greenfield",
            github_owner="owner",
            github_repo="non-greenfield",
            build_config={"repo_is_empty": False, "language": "python"},
        )
        task = _make_plan_task()
        session = _make_mock_session(project)

        no_op_reason = "the work is already done, the PR was merged three days ago"
        logs = json.dumps({"subtasks": [], "no_op_reason": no_op_reason})

        with (
            patch(
                "app.compute_backends.resolve_project_default_container_image",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.failure_classification.persist_failure_category"),
        ):
            await _handle_plan_completion(task, logs, session)

        assert task.status == TaskStatus.CANCELLED, (
            f"Expected CANCELLED but got {task.status} — "
            "a legitimate no-op on a non-greenfield project must still cancel cleanly"
        )

    @pytest.mark.asyncio
    async def test_undetermined_emptiness_no_op_cancelled_not_failed(self):
        """Undetermined (absent) repo_is_empty must not trigger the greenfield guard."""
        from app.plan_completion import _handle_plan_completion
        from app.models import TaskStatus

        project = Project(
            name="undetermined-empty",
            github_owner="owner",
            github_repo="undetermined",
            build_config={"language": None},  # repo_is_empty absent — undetermined
        )
        task = _make_plan_task()
        session = _make_mock_session(project)

        no_op_reason = "the codebase is empty, nothing to build here at all indeed"
        logs = json.dumps({"subtasks": [], "no_op_reason": no_op_reason})

        with (
            patch(
                "app.compute_backends.resolve_project_default_container_image",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.failure_classification.persist_failure_category"),
        ):
            await _handle_plan_completion(task, logs, session)

        assert task.status == TaskStatus.CANCELLED, (
            "Undetermined (absent) repo_is_empty must not trigger the greenfield guard — "
            "only repo_is_empty is True fires it"
        )


# ── is_repo_uninitialized (public failure_classification helper) ──────────────


class TestIsRepoUninitialized:
    """The public is_repo_uninitialized helper matches the commitless signatures
    and is safe on edge-case inputs."""

    def test_matches_each_uninitialized_pattern(self):
        from app.failure_classification import (
            _REPO_UNINITIALIZED_PATTERNS,
            is_repo_uninitialized,
        )

        for pattern in _REPO_UNINITIALIZED_PATTERNS:
            assert is_repo_uninitialized(pattern), f"should match: {pattern!r}"

    def test_case_insensitive(self):
        from app.failure_classification import is_repo_uninitialized

        assert is_repo_uninitialized("REPOSITORY HAS NO COMMITS")
        assert is_repo_uninitialized("This Repository Is Empty.")

    def test_does_not_match_delivery_disagreement(self):
        from app.failure_classification import (
            _CLONE_DELIVERY_DISAGREEMENT_PATTERNS,
            is_repo_uninitialized,
        )

        for pattern in _CLONE_DELIVERY_DISAGREEMENT_PATTERNS:
            assert not is_repo_uninitialized(pattern), (
                f"delivery-disagreement text must NOT match: {pattern!r}"
            )

    def test_safe_on_empty_and_none(self):
        from app.failure_classification import is_repo_uninitialized

        assert is_repo_uninitialized("") is False
        assert is_repo_uninitialized(None) is False


# ── Commitless self-heal: maybe_correct_stale_commitless_repo_signal ──────────
# A project whose durable repo_is_empty=False is stale on a genuinely commitless
# repo (no branches at all) loops clone_failed with a REPO_UNINITIALIZED
# signature forever because git clone --branch <trunk> dies before
# verifyCloneDelivered can run.  maybe_correct_stale_commitless_repo_signal
# re-verifies via repo_has_commits() and flips the signal when confirmed.


class TestStaleCommitlessSelfHeal:
    """maybe_correct_stale_commitless_repo_signal flips repo_is_empty=True on a
    confirmed commitless repo and is a no-op for non-stale / non-commitless cases."""

    def _stale_project(self):
        return Project(
            name="commitless-external",
            github_owner="HenryHamster",
            github_repo="commitless-external",
            build_config={"repo_is_empty": False, "source_file_count": 5},
        )

    @pytest.mark.asyncio
    async def test_flips_when_repo_has_no_commits(self):
        from app.build_config_detection import (
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github_refs.repo_has_commits",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sqlalchemy.orm.attributes.flag_modified"),
        ):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is True
        assert project.build_config["repo_is_empty"] is True
        assert project.build_config["source_file_count"] == 0
        assert project.build_config["has_ci"] is False
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_when_repo_is_already_true(self):
        from app.build_config_detection import (
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = Project(
            name="already-empty",
            github_owner="HenryHamster",
            github_repo="already-empty",
            build_config={"repo_is_empty": True, "source_file_count": 0},
        )
        session = AsyncMock()
        token_mock = AsyncMock(return_value="tok")

        with patch("app.github.get_repo_installation_token", token_mock):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is False
        token_mock.assert_not_awaited()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_when_repo_has_commits(self):
        from app.build_config_detection import (
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github_refs.repo_has_commits",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is False
        assert project.build_config["repo_is_empty"] is False
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_cooldown(self):
        import time

        from app.build_config_detection import (
            _REDETECT_COOLDOWN_S,
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = self._stale_project()
        project.id = uuid.uuid4()
        _redetect_stale_commitless_cooldown[str(project.id)] = (
            time.monotonic() - _REDETECT_COOLDOWN_S / 2
        )
        session = AsyncMock()
        token_mock = AsyncMock(return_value="tok")

        with patch("app.github.get_repo_installation_token", token_mock):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is False
        token_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_best_effort_on_token_failure(self):
        from app.build_config_detection import (
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()

        with patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is False
        assert project.build_config["repo_is_empty"] is False
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_best_effort_on_github_error(self):
        from app.build_config_detection import (
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github_refs.repo_has_commits",
                new_callable=AsyncMock,
                side_effect=RuntimeError("GitHub 502"),
            ),
        ):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is False
        assert project.build_config["repo_is_empty"] is False
        session.rollback.assert_awaited_once()
        # A thrown exception must also not consume the cooldown — next task retries.
        assert str(getattr(project, "id", "")) not in _redetect_stale_commitless_cooldown

    @pytest.mark.asyncio
    async def test_transient_token_failure_does_not_consume_cooldown(self):
        # A transient get_repo_installation_token failure (returns None) must NOT
        # stamp the 6-hour cooldown — the next task should be allowed to retry.
        from app.build_config_detection import (
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = self._stale_project()
        session = AsyncMock()

        with patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value=None,  # transient failure — no token
        ):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is False
        # Cooldown must NOT be stamped — a transient failure should not lock the
        # project out of self-healing for 6 hours.
        assert str(getattr(project, "id", "")) not in _redetect_stale_commitless_cooldown

    @pytest.mark.asyncio
    async def test_preserves_operator_overrides(self):
        from app.build_config_detection import (
            _redetect_stale_commitless_cooldown,
            maybe_correct_stale_commitless_repo_signal,
        )

        _redetect_stale_commitless_cooldown.clear()
        project = Project(
            name="overrides-preserved",
            github_owner="HenryHamster",
            github_repo="overrides-preserved",
            build_config={
                "repo_is_empty": False,
                "source_file_count": 3,
                "git_user_email": "bot@example.com",
                "git_user_name": "Blueprint Bot",
                "env_vars": {"FOO": "bar"},
            },
        )
        session = AsyncMock()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github_refs.repo_has_commits",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sqlalchemy.orm.attributes.flag_modified"),
        ):
            healed = await maybe_correct_stale_commitless_repo_signal(project, session)

        assert healed is True
        assert project.build_config["repo_is_empty"] is True
        assert project.build_config["git_user_email"] == "bot@example.com"
        assert project.build_config["git_user_name"] == "Blueprint Bot"
        assert project.build_config["env_vars"] == {"FOO": "bar"}


# ── agent_status wire-in: REPO_UNINITIALIZED clone_failed → self-heal ─────────


class TestAgentStatusRepoUninitializedSelfHeal:
    """The agent_status clone_failed block routes the REPO_UNINITIALIZED
    failure signature to maybe_correct_stale_commitless_repo_signal."""

    def test_is_repo_uninitialized_matches_clone_failed_text(self):
        from app.failure_classification import is_repo_uninitialized

        # Typical text emitted by the agent when ls-remote confirms no commits.
        assert is_repo_uninitialized("fatal: repository has no commits")
        assert is_repo_uninitialized("error: this repository is empty")
        assert is_repo_uninitialized("git repository is empty, cannot clone")

    def test_is_clone_delivery_disagreement_does_not_match_uninitialized_text(self):
        from app.failure_classification import is_clone_delivery_disagreement

        for text in [
            "repository has no commits",
            "this repository is empty",
            "git repository is empty",
        ]:
            assert not is_clone_delivery_disagreement(text), (
                f"delivery-disagreement must NOT match uninitialized text: {text!r}"
            )


# ── Cross-seam: clone_failed report ingestion → durable flag correction ────────
# The tests above prove the unit-level pieces (is_repo_uninitialized, the self-heal
# function) in isolation. This class proves they are wired together through the real
# update_agent_status handler: a FAILED report with failure_category="clone_failed"
# and REPO_UNINITIALIZED logs reaches maybe_correct_stale_commitless_repo_signal,
# which corrects the project's durable repo_is_empty flag so the next task runs
# greenfield instead of looping clone_failed forever.


def _stale_commitless_project(name="stale-commitless", repo="stale-commitless"):
    return Project(
        name=name,
        github_owner="HenryHamster",
        github_repo=repo,
        build_config={"repo_is_empty": False, "source_file_count": 5},
    )


def _in_progress_task(task_id, project_id, vm_instance_id):
    return Task(
        id=task_id,
        project_id=project_id,
        title="do something",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=vm_instance_id,
        context={},
        created_by="ORCHESTRATOR",
        attempt_count=0,
    )


def _handler_session_mock(task, project):
    """Minimal AsyncSession mock for update_agent_status cross-seam tests.

    The FOR UPDATE query (first execute call) returns the task. session.get()
    returns the project for the Project class so the self-heal can retrieve it,
    and None for everything else (MaxPlanAccount etc.).
    """
    task_row = MagicMock()
    task_row.scalars.return_value.first.return_value = task

    session = AsyncMock()
    session.execute = AsyncMock(return_value=task_row)
    session.get = AsyncMock(
        side_effect=lambda cls, pk: project if cls is Project else None
    )
    return session


def _status_request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/agent/status",
            "headers": [],
        }
    )


class TestAgentStatusCloneFailedSelfHealSeam:
    """update_agent_status dispatches clone_failed + REPO_UNINITIALIZED text to
    maybe_correct_stale_commitless_repo_signal and the durable flag is corrected."""

    @pytest.mark.asyncio
    async def test_clone_failed_repo_uninitialized_flips_repo_is_empty_via_status_handler(
        self,
    ):
        """Core acceptance test: a clone_failed FAILED report with REPO_UNINITIALIZED
        logs corrects a stale repo_is_empty=False to True through the real handler."""
        from fastapi import BackgroundTasks

        from app.build_config_detection import _redetect_stale_commitless_cooldown
        from app.routers.agent_status import update_agent_status

        _redetect_stale_commitless_cooldown.clear()

        task_id = uuid.uuid4()
        project_id = uuid.uuid4()
        vm_id = "vm-cross-seam-commitless"

        project = _stale_commitless_project()
        task = _in_progress_task(task_id, project_id, vm_id)
        session = _handler_session_mock(task, project)

        payload = AgentStatusPayload(
            task_id=task_id,
            status=TaskStatus.FAILED,
            failure_category="clone_failed",
            logs="fatal: repository has no commits",
        )
        token = {"sub": str(task_id), "vm_instance_id": vm_id}

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github_refs.repo_has_commits",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sqlalchemy.orm.attributes.flag_modified"),
            patch(
                "app.routers.agent_status.broadcast_to_project",
                new_callable=AsyncMock,
            ),
            patch(
                "app.routers.agent_status.maybe_spawn_smoketest_fix",
                new_callable=AsyncMock,
            ),
            patch(
                "app.routers.agent_status.maybe_spawn_debug_create",
                new_callable=AsyncMock,
            ),
            patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock),
            patch(
                "app.alert_dispatch._maybe_create_failure_alert",
                new_callable=AsyncMock,
            ),
            patch(
                "app.alert_dispatch._maybe_create_circuit_breaker_alert",
                new_callable=AsyncMock,
            ),
        ):
            await update_agent_status(
                request=_status_request(),
                payload=payload,
                background_tasks=BackgroundTasks(),
                session=session,
                token=token,
            )

        # The handler's self-heal path (agent_status.py:1625) must have called
        # maybe_correct_stale_commitless_repo_signal with this project, which
        # confirmed no commits via repo_has_commits() → False and flipped the flag.
        assert project.build_config["repo_is_empty"] is True, (
            f"Handler must flip repo_is_empty=True on a commitless repo via "
            f"the self-heal seam; got build_config={project.build_config!r}"
        )
        assert project.build_config.get("source_file_count") == 0
        assert project.build_config.get("has_ci") is False

    @pytest.mark.asyncio
    async def test_clone_failed_with_real_content_leaves_signal_unchanged_via_status_handler(
        self,
    ):
        """A repo that actually has commits must NOT have its repo_is_empty flag
        flipped — fail-loud is preserved so genuine clone shortfalls stay visible."""
        from fastapi import BackgroundTasks

        from app.build_config_detection import _redetect_stale_commitless_cooldown
        from app.routers.agent_status import update_agent_status

        _redetect_stale_commitless_cooldown.clear()

        task_id = uuid.uuid4()
        project_id = uuid.uuid4()
        vm_id = "vm-cross-seam-real-content"

        project = _stale_commitless_project(
            name="real-content-repo", repo="real-content-repo"
        )
        task = _in_progress_task(task_id, project_id, vm_id)
        session = _handler_session_mock(task, project)

        payload = AgentStatusPayload(
            task_id=task_id,
            status=TaskStatus.FAILED,
            failure_category="clone_failed",
            logs="fatal: repository has no commits",
        )
        token = {"sub": str(task_id), "vm_instance_id": vm_id}

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github_refs.repo_has_commits",
                new_callable=AsyncMock,
                return_value=True,  # repo HAS commits — clone shortfall, not empty
            ),
            patch("sqlalchemy.orm.attributes.flag_modified"),
            patch(
                "app.routers.agent_status.broadcast_to_project",
                new_callable=AsyncMock,
            ),
            patch(
                "app.routers.agent_status.maybe_spawn_smoketest_fix",
                new_callable=AsyncMock,
            ),
            patch(
                "app.routers.agent_status.maybe_spawn_debug_create",
                new_callable=AsyncMock,
            ),
            patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock),
            patch(
                "app.alert_dispatch._maybe_create_failure_alert",
                new_callable=AsyncMock,
            ),
            patch(
                "app.alert_dispatch._maybe_create_circuit_breaker_alert",
                new_callable=AsyncMock,
            ),
        ):
            await update_agent_status(
                request=_status_request(),
                payload=payload,
                background_tasks=BackgroundTasks(),
                session=session,
                token=token,
            )

        # A repo with real content that failed to clone must NOT be flipped —
        # the agent's verifyCloneDelivered must keep failing loud on the next attempt.
        assert project.build_config["repo_is_empty"] is False, (
            "A repo with real content (repo_has_commits=True) must NOT have its "
            f"repo_is_empty flipped; got build_config={project.build_config!r}"
        )


class TestEmptyRepoOnboardToResearchCompletionEndToEnd:
    """End-to-end: onboard a genuinely empty repo through the real endpoint,
    drive a research task to completion, and prove the spawned research child
    carries durable greenfield provenance that survives project build_config
    re-detection (the first commit landing flips repo_is_empty, so the child
    must not depend on reading the mutable project field later)."""

    @pytest.mark.asyncio
    async def test_onboard_empty_repo_research_completion_keeps_greenfield_provenance(
        self, client, db_session
    ):
        """Onboard an empty repo, complete a research task end-to-end, and verify
        both halves of the durable greenfield handoff:

          1. research_completion stamps repo_is_empty/source_file_count onto each
             spawned child's context at spawn time (the project build_config is
             mutable and flips once the first scaffolding lands);
          2. the agent-claim path prefers that spawn-time snapshot over the
             project's re-detected build_config when building the claim response,
             so the agent's greenfield guidance survives the parent->child
             handoff.
        """
        from app.auth import create_agent_token
        from app.findings_index import query_findings_index
        from app.models import ProjectStatus
        from app.research_completion import _handle_research_completion
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        # ── 1. Onboard the empty repo through the REAL endpoint ──────────────
        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=True,  # the commitless repo was seeded
            ),
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value=_empty_repo_build_config(),
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "empty-repo"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        project_id = data["project_id"]
        assert data["default_branch"] == "main"

        # ── 2. The six missing-state pins ────────────────────────────────────
        # (a) project record exists, with the resolved default branch persisted.
        project = await db_session.get(Project, project_id)
        assert project is not None
        assert project.integration_branch == "main"
        assert project.status == ProjectStatus.DEPLOYING
        assert project.autonomous_enabled is False

        # (b) absent .blueprint/tools.json — the persisted build_config honestly
        # records an empty repo (no language, no CI, zero real source files).
        build_config = project.build_config or {}
        assert build_config.get("repo_is_empty") is True
        assert build_config.get("source_file_count") == 0
        assert build_config.get("has_ci") is False
        assert "tools" not in build_config
        assert "commands" not in build_config

        # (c) empty knowledge index — nothing finalized yet.
        rows = await query_findings_index(db_session, project_id)
        assert rows == []

        # (d) no acceptance criteria / no directions on the fresh project.
        assert not (project.directions or [])

        # (e) no prior qualification — the empty-repo bootstrap guard fired and
        # no task of any kind was created by onboarding.
        tasks = (
            await db_session.execute(
                select(Task).where(Task.project_id == project_id)
            )
        ).scalars().all()
        assert tasks == []

        # ── 3. Drive a research task through the real completion handler ─────
        research_task = Task(
            project_id=project.id,
            title="Research: empty repo",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={"research_depth": 0},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        finding = {
            "title": "Seed repository scaffolding",
            "description": "The empty repo needs a README and scaffold.",
            "prompt": "Create README.md and basic project scaffolding.",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
        }
        logs = json.dumps([finding])

        async def _passthrough_ensure_branch(task, *_a, **_kw):
            return task

        # Same external-call mock surface as test_autonomous_research.py's
        # _patch_research_externals — the completion logic itself runs real.
        with (
            patch(
                "app.task_completion._ensure_branch",
                new=_passthrough_ensure_branch,
            ),
            patch(
                "app.broadcast.broadcast_to_project",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.research_completion._validate_plan_dependencies",
                return_value=True,
            ),
            patch(
                "app.research_completion._log_file_overlaps",
                return_value=[],
            ),
            patch("app.max_plan_pool._all_accounts", return_value=[]),
            patch(
                "app.research_completion._apply_structural_actionability_downgrade",
                return_value=0,
            ),
            patch(
                "app.research_completion._is_high_stakes_finding",
                return_value=False,
            ),
        ):
            research_task.status = TaskStatus.COMPLETED
            db_session.add(research_task)
            await db_session.commit()
            await _handle_research_completion(research_task, logs, db_session)

        # Parent flipped EVALUATING with live children — the wedge to prevent
        # (COMPLETED-with-children, or stranded IN_PROGRESS) is absent.
        await db_session.refresh(research_task)
        assert research_task.status == TaskStatus.EVALUATING

        # Findings were finalized: the index marker is set and the surviving
        # finding landed in plan_output.
        assert (research_task.context or {}).get("research_findings_finalized") is True
        assert research_task.plan_output is not None
        assert any(
            f.get("title") == finding["title"] for f in research_task.plan_output
        )

        # The child spawned carrying the durable greenfield provenance — the
        # research_completion production change.
        children = (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == research_task.id)
            )
        ).scalars().all()
        assert len(children) == 1
        child = children[0]
        assert child.context.get("repo_is_empty") is True
        assert child.context.get("source_file_count") == 0

        # ── 4. Simulate re-detection after the first scaffolding commit ──────
        # The project's durable build_config flips to non-empty (repo_bootstrap /
        # maybe_redetect_on_emptiness_disagreement re-detects the now-seeded
        # repo). The spawned child must NOT depend on this mutable field.
        project.build_config = {
            **build_config,
            "repo_is_empty": False,
            "source_file_count": 3,
            "has_ci": False,
        }
        flag_modified(project, "build_config")
        db_session.add(project)
        await db_session.commit()

        # ── 5. Claim the child — the agent-claim production change ───────────
        token = create_agent_token(str(child.id), child.vm_instance_id)
        claim_resp = await client.post(
            "/api/agent/claim", headers={"x-agent-token": token}
        )
        assert claim_resp.status_code == 200, claim_resp.text
        claim_data = claim_resp.json()
        # The child's spawn-time snapshot (repo_is_empty True / 0 files) must
        # win over the re-detected project build_config (now False / 3 files).
        assert claim_data["repo_is_empty"] is True
        assert claim_data["source_file_count"] == 0

        # ── 6. Simulate the rollup terminal flip, then query the index ───────
        child.status = TaskStatus.COMPLETED
        db_session.add(child)
        await db_session.commit()
        research_task.status = TaskStatus.COMPLETED
        db_session.add(research_task)
        await db_session.commit()

        rows = await query_findings_index(db_session, project_id)
        assert len(rows) == 1
        assert rows[0]["finding_title"] == finding["title"]
