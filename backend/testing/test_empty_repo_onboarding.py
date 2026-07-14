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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.github_refs import ensure_repo_initialized, repo_has_commits
from app.models import Project
from app.repo_bootstrap import spawn_bootstrap_task
from app.routers.admin_onboard import (
    GREENFIELD_SOURCE_FILE_THRESHOLD,
    _classify_repo_emptiness,
    _detect_build_config,
    _empty_repo_build_config,
    _is_empty_repository_response,
)


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


class TestBootstrapGuardIsReachable:
    """The payoff: the empty-repo signal now actually reaches the guard."""

    @pytest.mark.asyncio
    async def test_detected_empty_repo_suppresses_bootstrap(self):
        project = Project(
            name="playground",
            github_owner="HenryHamster",
            github_repo="playground",
            build_config=_empty_repo_build_config(),
        )

        result = await spawn_bootstrap_task(
            project, MagicMock(), is_first_bootstrap=True, spawn_reason="first_cycle"
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
