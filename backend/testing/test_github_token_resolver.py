"""Seam + token-freshness tests for the extracted GitHub token resolver module.

Verifies that ``app.github_token_resolver`` and the legacy
``app.routers.agent`` re-export path expose the SAME objects (identical
function/cache/lock instances), so the behavior-preserving extraction did not
fork state, and that importing the resolver module does not require importing
the agent FastAPI router (no router-level coupling / circular import).

Also guards the token-freshness invariant: a token handed to a caller always has
a real margin of remaining life, no cache serves a token past its TRUE expiry,
and none of that costs extra mints against the shared 5,000/hr installation
budget.
"""

import asyncio
import importlib
import subprocess
import sys

import pytest

from app.models import Project, Task, UserIntegration


def test_resolver_symbols_are_same_objects_via_both_paths():
    import app.github_token_resolver as resolver
    import app.routers.agent as agent

    # Identical object identity across both import paths — the router merely
    # re-exports; it must not define its own copy that could drift.
    assert agent.get_github_token_for_task is resolver.get_github_token_for_task
    assert agent.invalidate_github_token_cache is resolver.invalidate_github_token_cache
    assert agent._github_token_cache is resolver._github_token_cache
    assert agent._github_token_cache_lock is resolver._github_token_cache_lock
    assert agent._GITHUB_TOKEN_TTL == resolver._GITHUB_TOKEN_TTL


def test_cache_is_shared_state_not_a_copy():
    """A mutation through one path is visible through the other (same dict)."""
    import app.github_token_resolver as resolver
    import app.routers.agent as agent

    sentinel_key = "__seam_test_sentinel_project__"
    try:
        resolver._github_token_cache[sentinel_key] = {"token": "x"}
        assert agent._github_token_cache.get(sentinel_key) == {"token": "x"}

        agent.invalidate_github_token_cache(sentinel_key)
        assert sentinel_key not in resolver._github_token_cache
    finally:
        resolver._github_token_cache.pop(sentinel_key, None)


def test_resolver_module_does_not_pull_in_agent_router():
    """The resolver must be importable WITHOUT importing app.routers.agent.

    This is the whole point of the extraction: the 15+ non-router modules that
    only need a GitHub token should not transitively depend on the agent
    FastAPI router. Run in a fresh interpreter so prior imports don't mask it.
    """
    code = (
        "import sys; import app.github_token_resolver; "
        "assert 'app.routers.agent' not in sys.modules, "
        "'github_token_resolver must not import app.routers.agent'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_no_circular_import_on_reimport():
    """Re-importing both modules in either order does not raise."""
    importlib.reload(importlib.import_module("app.github_token_resolver"))
    importlib.import_module("app.routers.agent")


# ---------------------------------------------------------------------------
# Token freshness: no cache may serve a token past its TRUE expiry
# ---------------------------------------------------------------------------

_LIFETIME = 3600  # GitHub installation access token lifetime (60 min)


class _FakeClock:
    """Controllable stand-in for github_auth's ``time`` module."""

    def __init__(self, start: float = 10_000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _MockResponse:
    """Minimal httpx-response stand-in (local, so this file stays self-contained)."""

    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = ""
        self.headers = {}
        self.links = {}

    def json(self):
        return self._json_data


class _MintRecorder:
    """Records every access-token mint, so tests can assert on mint VOLUME."""

    def __init__(self, clock: _FakeClock):
        self.clock = clock
        self.mints: list[tuple[str, float]] = []  # (token, minted_at)

    def minted_at(self, token: str) -> float:
        for name, ts in self.mints:
            if name == token:
                return ts
        raise AssertionError(f"{token} was never minted by this recorder")

    def remaining_life(self, token: str) -> float:
        """True seconds of life left on ``token`` at the clock's current time."""
        return _LIFETIME - (self.clock.now - self.minted_at(token))


@pytest.fixture
def token_env(mocker):
    """Wire github_auth to a fake clock + recording transport, with clean caches."""
    import app.github_auth as gh_auth
    import app.github_token_resolver as resolver

    def _reset():
        gh_auth._repo_installation_token_cache.clear()
        gh_auth._repo_installation_read_token_cache.clear()
        gh_auth._installation_id_cache.clear()
        resolver._github_token_cache.clear()
        # getattr-guarded: these two are introduced by the fix. Referencing them
        # bare would raise AttributeError during FIXTURE SETUP when this file is
        # run against pre-fix code, turning every regression test in it into a
        # setup ERROR — so the real assertion message ("served a token minted 65
        # min ago...") would never be reached and the tests would prove nothing.
        # Guarding here keeps them failing CLEANLY, on their actual assertions.
        getattr(resolver, "_project_repo_index", {}).clear()
        getattr(resolver, "_project_mint_locks", {}).clear()

    _reset()

    clock = _FakeClock()
    recorder = _MintRecorder(clock)

    # A plain async function — NOT an object with an async __call__. AsyncMock
    # only awaits a side_effect's result when the side_effect itself is a
    # coroutine function, which a callable instance is not.
    async def _fake_request(method, url, headers, **kwargs):
        if url.endswith("/access_tokens"):
            token = f"ghs_mint_{len(recorder.mints) + 1}"
            recorder.mints.append((token, clock.now))
            return _MockResponse(201, {"token": token})
        return _MockResponse(404, {})

    mocker.patch.object(gh_auth, "time", clock)
    mocker.patch.object(gh_auth, "GITHUB_APP_ID", "123")
    mocker.patch.object(gh_auth, "GITHUB_PRIVATE_KEY", "key")
    mocker.patch.object(gh_auth, "generate_github_app_jwt", return_value="jwt")
    mocker.patch("app.github._github_request", side_effect=_fake_request)

    yield clock, recorder

    _reset()


async def _make_task(db_session, *, owner, repo, user_id, integration=None):
    project = Project(
        name=f"tok-{user_id}",
        user_id=user_id,
        github_owner=owner,
        github_repo=repo,
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    if integration is not None:
        db_session.add(integration)
        await db_session.commit()

    task = Task(project_id=project.id, payloads=[{"type": "create"}])
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


def _seed_installation(owner, repo, clock):
    """Pre-seed the installation id so token minting is the only GitHub call."""
    import app.github_auth as gh_auth

    gh_auth._installation_id_cache[f"{owner}/{repo}"] = ("555", clock.time())


@pytest.mark.asyncio
async def test_resolver_never_serves_a_token_past_its_true_expiry(
    db_session, token_env
):
    """REGRESSION: a token minted at T-45min must not still be served at T+20min.

    The stacked-cache bug: github_auth caches a minted token for 50 min (anchored
    at MINT time — correct). The resolver used to wrap that in a SECOND 50-min TTL
    anchored at the moment IT inserted the entry. So a token that was already
    45 min old when the resolver first saw it got served for another 50 min —
    i.e. well past its 60-min expiry.

    The timeline below reproduces exactly that: the repo cache is warmed by a
    non-resolver caller (auto_remediation / branch_cleanup do this constantly),
    the resolver first sees the token at t=45min, and by t=65min it is DEAD.
    """
    import app.github_auth as gh_auth
    from app.github_token_resolver import get_github_token_for_task

    clock, recorder = token_env
    _seed_installation("acme", "widget", clock)
    _, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-expiry"
    )

    # t=0 — some OTHER caller warms github_auth's per-repo cache.
    first = await gh_auth.get_repo_installation_token("acme", "widget")
    assert first == "ghs_mint_1"

    # t=45min — resolver's first look. The token is 45 min old but still has
    # 15 min of life, so serving it is legitimate.
    clock.advance(45 * 60)
    token, _, _ = await get_github_token_for_task(task.id, db_session)
    assert token == "ghs_mint_1"
    assert recorder.remaining_life(token) >= 600

    # t=65min — ghs_mint_1 is now DEAD (65 > 60). The old resolver cache would
    # have considered it only 20 min old and happily served it. It must re-mint.
    clock.advance(20 * 60)
    token, _, _ = await get_github_token_for_task(task.id, db_session)

    assert (
        token != "ghs_mint_1"
    ), "resolver served a token minted 65 min ago — past its 60-min expiry"
    assert token == "ghs_mint_2"
    assert recorder.remaining_life(token) == _LIFETIME


@pytest.mark.asyncio
async def test_every_served_token_has_the_freshness_margin(db_session, token_env):
    """Across a long sweep of calls, no token is ever handed out with < 10 min left."""
    from app.github_token_resolver import (
        _TOKEN_MIN_REMAINING_LIFE,
        get_github_token_for_task,
    )

    clock, recorder = token_env
    _seed_installation("acme", "widget", clock)
    _, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-margin"
    )

    # Walk 3 hours in 5-minute steps — crosses several re-mint boundaries.
    for _ in range(36):
        token, _, _ = await get_github_token_for_task(task.id, db_session)
        assert (
            recorder.remaining_life(token) >= _TOKEN_MIN_REMAINING_LIFE
        ), f"served {token} with only {recorder.remaining_life(token)}s of life left"
        clock.advance(5 * 60)


@pytest.mark.asyncio
async def test_app_discovery_token_is_not_double_cached_by_resolver(
    db_session, token_env
):
    """STRUCTURAL GUARD: the resolver must not keep its own copy of a repo token.

    github_auth's per-repo cache is the single authority for App-discovery tokens
    (it is mint-anchored and single-flighted). Storing a second copy here, keyed
    on insert time, is what re-opens the stale window — so assert it stays empty.
    """
    import app.github_token_resolver as resolver
    from app.github_token_resolver import get_github_token_for_task

    clock, _ = token_env
    _seed_installation("acme", "widget", clock)
    project, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-nodouble"
    )

    token, _, _ = await get_github_token_for_task(task.id, db_session)
    assert token == "ghs_mint_1"

    assert str(project.id) not in resolver._github_token_cache, (
        "resolver re-introduced the redundant per-project copy of an App-discovery "
        "token — this is the stacked-TTL bug"
    )
    # ...but the cheap project -> repo resolution IS retained, so invalidation
    # can still reach the repo cache.
    assert resolver._project_repo_index[str(project.id)] == ("acme", "widget")


@pytest.mark.asyncio
async def test_dedicated_resolver_session_releases_connection_before_external_io(
    db_session, monkeypatch
):
    """Resident inspections must not pin a DB connection during token discovery."""
    import app.github as github
    from app.github_token_resolver import get_github_token_for_task

    _, task = await _make_task(
        db_session, owner="acme", repo="detached", user_id="u-detached"
    )

    async def resolve_after_release(owner, repo):
        assert (owner, repo) == ("acme", "detached")
        assert not db_session.in_transaction()
        return "ghs_detached"

    monkeypatch.setattr(github, "get_repo_installation_token", resolve_after_release)

    token, owner, repo = await get_github_token_for_task(
        task.id,
        db_session,
        release_dedicated_session_before_io=True,
    )

    assert (token, owner, repo) == ("ghs_detached", "acme", "detached")


@pytest.mark.asyncio
async def test_dedicated_resolver_rejects_pending_state_before_any_query(
    db_session, monkeypatch
):
    """Release mode must fail before autoflush can hide caller-owned writes."""
    from unittest.mock import AsyncMock

    import app.github_token_resolver as resolver

    pending = UserIntegration(user_id="u-pending-detached")
    db_session.add(pending)
    execute = AsyncMock(side_effect=AssertionError("resolver executed SQL"))
    monkeypatch.setattr(db_session, "execute", execute)

    with pytest.raises(RuntimeError, match="clean dedicated session"):
        await resolver.get_github_token_for_task(
            "00000000-0000-0000-0000-000000000001",
            db_session,
            release_dedicated_session_before_io=True,
        )

    execute.assert_not_awaited()
    assert pending in db_session.new


@pytest.mark.asyncio
async def test_dedicated_resolver_releases_before_fallback_mint(
    db_session, monkeypatch
):
    """Stored-installation fallback also runs after the read transaction ends."""
    import app.github as github
    import app.github_token_resolver as resolver

    project, task = await _make_task(
        db_session,
        owner="acme",
        repo="fallback-detached",
        user_id="u-fallback-detached",
        integration=UserIntegration(
            user_id="u-fallback-detached",
            github_installation_id="installation-7",
        ),
    )

    async def unavailable_app_token(owner, repo):
        assert not db_session.in_transaction()
        return None

    async def mint_after_release(installation_id, *, repositories):
        assert installation_id == "installation-7"
        assert repositories == ["fallback-detached"]
        assert not db_session.in_transaction()
        return "ghs_fallback_detached"

    monkeypatch.setattr(github, "get_repo_installation_token", unavailable_app_token)
    monkeypatch.setattr(
        resolver,
        "generate_installation_access_token",
        mint_after_release,
    )

    try:
        token, owner, repo = await resolver.get_github_token_for_task(
            task.id,
            db_session,
            release_dedicated_session_before_io=True,
        )

        assert (token, owner, repo) == (
            "ghs_fallback_detached",
            "acme",
            "fallback-detached",
        )
    finally:
        project_id = str(project.id)
        resolver._github_token_cache.pop(project_id, None)
        resolver.forget_project_repo(project_id)


# ---------------------------------------------------------------------------
# Mint budget: the shared 5,000/hr installation-token bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_calls_do_not_increase_mint_volume(db_session, token_env):
    """20 resolves inside the freshness window cost exactly ONE mint."""
    from app.github_token_resolver import get_github_token_for_task

    clock, recorder = token_env
    _seed_installation("acme", "widget", clock)
    _, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-volume"
    )

    for _ in range(20):
        token, _, _ = await get_github_token_for_task(task.id, db_session)
        assert token == "ghs_mint_1"
        clock.advance(60)  # 20 minutes of polling

    assert (
        len(recorder.mints) == 1
    ), f"expected 1 mint per repo per 50 min, got {len(recorder.mints)}"


@pytest.mark.asyncio
async def test_concurrent_burst_coalesces_to_a_single_mint(db_session, token_env):
    """A cold-cache burst is single-flighted — 12 concurrent resolves, 1 mint."""
    from app.github_token_resolver import get_github_token_for_task

    clock, recorder = token_env
    _seed_installation("acme", "widget", clock)
    _, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-burst"
    )

    results = await asyncio.gather(
        *(get_github_token_for_task(task.id, db_session) for _ in range(12))
    )

    assert {t for t, _, _ in results} == {"ghs_mint_1"}
    assert (
        len(recorder.mints) == 1
    ), f"single-flight broken: burst minted {len(recorder.mints)} tokens"


@pytest.mark.asyncio
async def test_mint_rate_is_one_per_repo_per_50_min(db_session, token_env):
    """Steady-state ceiling: 3h of continuous polling => 4 mints (t=0, 50, 100, 150)."""
    from app.github_token_resolver import get_github_token_for_task

    clock, recorder = token_env
    _seed_installation("acme", "widget", clock)
    _, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-rate"
    )

    for _ in range(180):  # 180 one-minute polls == 3 hours
        await get_github_token_for_task(task.id, db_session)
        clock.advance(60)

    assert len(recorder.mints) == 4, (
        f"mint rate regressed: {len(recorder.mints)} mints in 3h "
        "(expected 1 per repo per 50 min)"
    )


# ---------------------------------------------------------------------------
# Self-minted fallback tokens (stored-installation / PAT) — the paths that have
# no other cache, so the resolver MUST keep caching them.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stored_installation_fallback_is_cached_and_mint_anchored(
    db_session, token_env, mocker
):
    """The fallback path has no downstream cache — dropping it would mint per call."""
    import app.github_token_resolver as resolver
    from app.github_token_resolver import get_github_token_for_task

    _clock, recorder = token_env
    # App-level discovery unavailable => fall through to the stored installation.
    mocker.patch(
        "app.github.get_repo_installation_token",
        side_effect=Exception("no app install"),
    )

    project, task = await _make_task(
        db_session,
        owner="fb",
        repo="repo",
        user_id="u-fallback",
        integration=UserIntegration(user_id="u-fallback", github_installation_id="i-9"),
    )

    t1, _, _ = await get_github_token_for_task(task.id, db_session)
    t2, _, _ = await get_github_token_for_task(task.id, db_session)

    assert t1 == t2 == "ghs_mint_1"
    assert len(recorder.mints) == 1, "fallback path lost its cache — mints every call"

    # It IS cached here (unlike App-discovery tokens) and carries a true expiry.
    entry = resolver._github_token_cache[str(project.id)]
    assert entry["expires_at"] > entry["fetched_at"]
    assert (entry["expires_at"] - entry["fetched_at"]).total_seconds() == _LIFETIME


@pytest.mark.asyncio
async def test_expired_self_minted_entry_is_not_served():
    """A self-minted entry whose TRUE expiry has passed is evicted, not served."""
    from datetime import timedelta

    import app.github_token_resolver as resolver

    project_id = "proj-stale-sentinel"
    now = resolver._utcnow()
    try:
        # Minted 55 min ago: only 5 min of life left, under the 10-min margin,
        # so it must NOT be served.
        resolver._github_token_cache[project_id] = {
            "token": "ghs_nearly_dead",
            "owner": "acme",
            "repo": "widget",
            "fetched_at": now - timedelta(minutes=55),
            "expires_at": now + timedelta(minutes=5),
        }
        assert resolver.get_cached_project_token(project_id) is None
        assert (
            project_id not in resolver._github_token_cache
        ), "stale entry must be evicted, not left to be served later"

        # A comfortably-fresh entry IS served.
        resolver._github_token_cache[project_id] = {
            "token": "ghs_fresh",
            "owner": "acme",
            "repo": "widget",
            "fetched_at": now,
            "expires_at": now + timedelta(minutes=60),
        }
        assert resolver.get_cached_project_token(project_id)["token"] == "ghs_fresh"
    finally:
        resolver._github_token_cache.pop(project_id, None)


@pytest.mark.asyncio
async def test_invalidate_forces_remint_through_the_repo_cache(db_session, token_env):
    """401 self-heal: invalidate must reach github_auth's cache, otherwise the
    retry is handed the very same token that just 401'd."""
    from app.github_token_resolver import (
        get_github_token_for_task,
        invalidate_github_token_cache,
    )

    clock, recorder = token_env
    _seed_installation("acme", "widget", clock)
    project, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-invalidate"
    )

    t1, _, _ = await get_github_token_for_task(task.id, db_session)
    assert t1 == "ghs_mint_1"

    invalidate_github_token_cache(str(project.id))

    t2, _, _ = await get_github_token_for_task(task.id, db_session)
    assert t2 == "ghs_mint_2", "invalidation did not force a re-mint"
    assert len(recorder.mints) == 2


# ---------------------------------------------------------------------------
# Health signal: the resolver must not forge GitHub reachability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_discovery_path_does_not_stamp_github_health(db_session, token_env):
    """FAIL-TOWARD-GREEN GUARD: a warm-cache resolve must not forge a health signal.

    ``check_github_health()`` short-circuits — skipping its ACTIVE probe entirely
    — whenever ``_shared_state.last_github_success`` is younger than 60s. The
    App-discovery return path is taken on ~100% of resolver calls for App
    projects, and while github_auth's repo cache is warm it performs ZERO GitHub
    network I/O: it is a dict lookup. If it stamped that marker, every resolver
    call would re-assert "GitHub is up" from local memory, and the health check
    would keep reporting HEALTHY for the whole cache window into a real outage.

    Only a genuine round-trip may stamp the marker.
    """
    from datetime import timedelta

    import app.shared_state as shared_state
    from app.github_token_resolver import _utcnow, get_github_token_for_task

    clock, recorder = token_env
    _seed_installation("acme", "widget", clock)
    _, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-health"
    )

    previous = shared_state.last_github_success
    try:
        # Cold resolve: this one really does mint, i.e. real GitHub network I/O.
        await get_github_token_for_task(task.id, db_session)
        assert len(recorder.mints) == 1

        # github_auth's repo cache is now warm. Simulate GitHub having gone down
        # 10 minutes ago by backdating the marker, then resolve again. This call
        # touches nothing on the network, so it must leave the marker untouched.
        stale = _utcnow() - timedelta(minutes=10)
        shared_state.last_github_success = stale

        token, _, _ = await get_github_token_for_task(task.id, db_session)

        assert token == "ghs_mint_1", "expected a warm-cache hit, not a re-mint"
        assert len(recorder.mints) == 1, "warm path unexpectedly minted"
        assert shared_state.last_github_success == stale, (
            "resolver stamped last_github_success on a path that made ZERO GitHub "
            "calls — check_github_health() will now skip its active probe and "
            "report GitHub HEALTHY straight through an outage"
        )
    finally:
        shared_state.last_github_success = previous


@pytest.mark.asyncio
async def test_fallback_mint_burst_coalesces_to_a_single_mint(
    db_session, token_env, mocker
):
    """Single-flight on the SELF-MINTED fallback path, not just App-discovery.

    ``_github_token_cache_lock`` guards the cache DICT; the fallback mint happens
    outside it. Without a per-project mint lock, a cold burst of N callers on one
    project fires N mints at the shared 5,000/hr bucket.
    """
    from app.github_token_resolver import get_github_token_for_task

    _clock, recorder = token_env
    mocker.patch(
        "app.github.get_repo_installation_token",
        side_effect=Exception("no app install"),
    )

    _, task = await _make_task(
        db_session,
        owner="fb",
        repo="burst",
        user_id="u-fbburst",
        integration=UserIntegration(user_id="u-fbburst", github_installation_id="i-9"),
    )

    results = await asyncio.gather(
        *(get_github_token_for_task(task.id, db_session) for _ in range(12))
    )

    assert {t for t, _, _ in results} == {"ghs_mint_1"}
    assert len(recorder.mints) == 1, (
        "fallback single-flight broken: a 12-caller cold burst minted "
        f"{len(recorder.mints)} tokens against the shared 5,000/hr bucket"
    )


@pytest.mark.asyncio
async def test_forget_project_repo_drops_the_stale_mapping(db_session, token_env):
    """A deleted project must not leave a mapping that still reaches its old repo."""
    import app.github_token_resolver as resolver
    from app.github_token_resolver import (
        forget_project_repo,
        get_github_token_for_task,
    )

    clock, _ = token_env
    _seed_installation("acme", "widget", clock)
    project, task = await _make_task(
        db_session, owner="acme", repo="widget", user_id="u-forget"
    )

    await get_github_token_for_task(task.id, db_session)
    assert resolver._project_repo_index[str(project.id)] == ("acme", "widget")

    forget_project_repo(str(project.id))

    assert str(project.id) not in resolver._project_repo_index, (
        "stale project -> repo mapping survives deletion; a later invalidate for "
        "this id would reach into a repo the project no longer owns"
    )
    assert str(project.id) not in resolver._project_mint_locks
