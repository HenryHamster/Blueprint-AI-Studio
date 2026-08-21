"""Tests for app.staging_base_refresh — keeping research/plan bases current.

The defect under test: a research parent cuts ``research/{short_id}`` once at
spawn and hands that name to every child as ``base_branch``. Integration keeps
advancing, so later children are graded against a frozen tree and report
blockers for failures that were already fixed on integration. The sweep merges
integration INTO each staging base that still has pending children.
"""

import asyncio
import types


class _StubProject:
    """Minimal Project stand-in: only what the sweep actually reads."""

    def __init__(
        self,
        *,
        integration_branch="automain",
        staging_prefixes=("research/", "plan/"),
        owner="HenryHamster",
        repo="BlueprintAIStudio",
    ):
        self.id = "11111111-1111-1111-1111-111111111111"
        self.user_id = "22222222-2222-2222-2222-222222222222"
        self.integration_branch = integration_branch
        self.github_owner = owner
        self.github_repo = repo
        self._staging_prefixes = staging_prefixes

    def is_staging_branch(self, branch):
        if not branch:
            return False
        return branch.startswith(tuple(self._staging_prefixes))


def _task(base_branch):
    """A Task stand-in exposing only ``first_payload``."""
    payload = {} if base_branch is None else {"base_branch": base_branch}
    return types.SimpleNamespace(first_payload=payload)


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _StubSession:
    """Returns a fixed row set for the sweep's single SELECT."""

    def __init__(self, rows):
        self._rows = rows
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        return _StubResult(self._rows)


class _RecordingMerge:
    """Stands in for ``app.github.merge_branch`` and records every call."""

    def __init__(self, results):
        # results: branch -> dict returned by merge_branch (or an Exception to raise)
        self._results = results
        self.calls = []

    async def __call__(self, token, owner, repo, *, base, head, **kwargs):
        self.calls.append(
            {
                "token": token,
                "owner": owner,
                "repo": repo,
                "base": base,
                "head": head,
                **kwargs,
            }
        )
        outcome = self._results.get(base, {"merged": True, "already": False})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run_project_sweep(project, tasks, merge_results=None, monkeypatch=None):
    """Drive ``_refresh_project_staging_bases`` with a stubbed merge_branch."""
    import app.github as github_mod
    from app.staging_base_refresh import _refresh_project_staging_bases

    recorder = _RecordingMerge(merge_results or {})
    original = github_mod.merge_branch
    github_mod.merge_branch = recorder
    try:
        session = _StubSession(tasks)
        counts = asyncio.run(
            _refresh_project_staging_bases(project, "gh-token", session)
        )
    finally:
        github_mod.merge_branch = original
    return counts, recorder


# ── collect_pending_staging_bases (pure selection rule) ──────────────────────


def test_collect_dedupes_shared_staging_base():
    """Many children of one parent share one branch — one merge, not N."""
    from app.staging_base_refresh import collect_pending_staging_bases

    project = _StubProject()
    tasks = [_task("research/2bde59b0") for _ in range(12)]
    assert collect_pending_staging_bases(project, tasks) == {"research/2bde59b0"}


def test_collect_excludes_non_staging_bases():
    """Integration, task and release branches are never moved by this sweep."""
    from app.staging_base_refresh import collect_pending_staging_bases

    project = _StubProject()
    tasks = [
        _task("automain"),
        _task("main"),
        _task("blueprint/task-abcdef12"),
        _task("release-v35"),
        _task("research/2bde59b0"),
        _task("plan/deadbeef"),
    ]
    assert collect_pending_staging_bases(project, tasks) == {
        "research/2bde59b0",
        "plan/deadbeef",
    }


def test_collect_tolerates_missing_and_non_string_bases():
    """A payload with no base_branch (or a junk value) is skipped, not fatal."""
    from app.staging_base_refresh import collect_pending_staging_bases

    project = _StubProject()
    tasks = [
        _task(None),
        _task(""),
        types.SimpleNamespace(first_payload=None),
        types.SimpleNamespace(first_payload={"base_branch": 17}),
        _task("research/2bde59b0"),
    ]
    assert collect_pending_staging_bases(project, tasks) == {"research/2bde59b0"}


def test_collect_honours_custom_staging_prefixes():
    """Non-Blueprint installs with custom prefixes are classified correctly."""
    from app.staging_base_refresh import collect_pending_staging_bases

    project = _StubProject(staging_prefixes=("feature/", "experiment/"))
    tasks = [
        _task("feature/abcdef12"),
        _task("research/2bde59b0"),  # not a staging prefix for THIS project
    ]
    assert collect_pending_staging_bases(project, tasks) == {"feature/abcdef12"}


# ── merge orientation (the invariant that must never regress) ────────────────


def test_merge_direction_is_integration_into_staging_never_the_reverse():
    """base=staging, head=integration.

    Reversing these would push unreviewed research rollup work straight onto
    the integration trunk. Pinned with literals on purpose.
    """
    project = _StubProject(integration_branch="automain")
    counts, recorder = _run_project_sweep(project, [_task("research/2bde59b0")])

    assert counts["refreshed"] == 1
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["base"] == "research/2bde59b0"
    assert call["head"] == "automain"
    assert call["owner"] == "HenryHamster"
    assert call["repo"] == "BlueprintAIStudio"
    assert call["token"] == "gh-token"


def test_integration_branch_is_never_merged_into_itself():
    """A project whose integration branch itself carries a staging prefix."""
    project = _StubProject(
        integration_branch="research/trunk", staging_prefixes=("research/",)
    )
    counts, recorder = _run_project_sweep(
        project, [_task("research/trunk"), _task("research/2bde59b0")]
    )

    assert [c["base"] for c in recorder.calls] == ["research/2bde59b0"]
    assert counts["refreshed"] == 1


def test_one_merge_per_distinct_branch_not_per_task():
    """GitHub cost is proportional to branches, not to fan-out width."""
    project = _StubProject()
    tasks = [_task("research/2bde59b0") for _ in range(25)] + [
        _task("plan/deadbeef") for _ in range(25)
    ]
    counts, recorder = _run_project_sweep(project, tasks)

    assert len(recorder.calls) == 2
    assert sorted(c["base"] for c in recorder.calls) == [
        "plan/deadbeef",
        "research/2bde59b0",
    ]
    assert counts["refreshed"] == 2


# ── outcome classification ───────────────────────────────────────────────────


def test_already_up_to_date_is_not_counted_as_a_refresh():
    """A 204 (base already contains integration) is a no-op, not a change."""
    project = _StubProject()
    counts, _ = _run_project_sweep(
        project,
        [_task("research/2bde59b0")],
        merge_results={"research/2bde59b0": {"merged": True, "already": True}},
    )
    assert counts == {
        "refreshed": 0,
        "already": 1,
        "conflict": 0,
        "missing": 0,
        "error": 0,
    }


def test_conflict_leaves_the_branch_alone_and_is_reported():
    """A 409 must not be retried into a destructive repair — just reported."""
    project = _StubProject()
    counts, recorder = _run_project_sweep(
        project,
        [_task("research/2bde59b0")],
        merge_results={"research/2bde59b0": {"merged": False, "conflict": True}},
    )
    assert counts["conflict"] == 1
    assert counts["refreshed"] == 0
    # Exactly one attempt; no delete/recreate fallback exists in this module.
    assert len(recorder.calls) == 1


def test_missing_branch_is_classified_separately_from_error():
    """A cleaned-up staging branch (404) is permanent, not a transient error."""
    project = _StubProject()
    counts, _ = _run_project_sweep(
        project,
        [_task("research/2bde59b0")],
        merge_results={"research/2bde59b0": {"merged": False, "missing": True}},
    )
    assert counts["missing"] == 1
    assert counts["error"] == 0


def test_one_branch_raising_does_not_abort_the_rest_of_the_sweep():
    """A transport failure on one branch must not strand its siblings."""
    project = _StubProject()
    counts, recorder = _run_project_sweep(
        project,
        [_task("research/2bde59b0"), _task("plan/deadbeef")],
        merge_results={"plan/deadbeef": RuntimeError("connection reset")},
    )
    assert counts["error"] == 1
    assert counts["refreshed"] == 1
    assert len(recorder.calls) == 2


def test_no_pending_tasks_means_no_github_calls():
    """The sweep costs nothing when nothing is waiting on a staging base."""
    project = _StubProject()
    counts, recorder = _run_project_sweep(project, [_task("automain")])
    assert recorder.calls == []
    assert counts == {
        "refreshed": 0,
        "already": 0,
        "conflict": 0,
        "missing": 0,
        "error": 0,
    }


# ── sweep-level gates ────────────────────────────────────────────────────────


def test_pending_statuses_exclude_running_and_terminal_work():
    """Only statuses whose NEXT dispatch re-reads base_branch are swept.

    IN_PROGRESS/EVALUATING agents already hold a clone (a refresh cannot reach
    them); terminal tasks will never read the base again.
    """
    from app.models import TaskStatus
    from app.staging_base_refresh import _PENDING_STATUSES

    assert set(_PENDING_STATUSES) == {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
    for excluded in (
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ):
        assert excluded not in _PENDING_STATUSES


def test_low_install_token_quota_skips_the_whole_cycle():
    """A base refresh must never be the call that starves task spawning."""
    import app.shared_state as shared_state
    from app.staging_base_refresh import _QUOTA_FLOOR, refresh_stale_staging_bases

    saved = dict(shared_state.github_quota)
    try:
        shared_state.github_quota["remaining"] = _QUOTA_FLOOR - 1
        totals = asyncio.run(refresh_stale_staging_bases())
    finally:
        shared_state.github_quota.clear()
        shared_state.github_quota.update(saved)

    # Returned before opening a session or touching GitHub.
    assert totals == {
        "refreshed": 0,
        "already": 0,
        "conflict": 0,
        "missing": 0,
        "error": 0,
    }


def test_low_jwt_quota_skips_the_whole_cycle():
    """The ~60/hr App-JWT bucket gets the same protection."""
    import app.shared_state as shared_state
    from app.staging_base_refresh import _JWT_QUOTA_FLOOR, refresh_stale_staging_bases

    saved_jwt = dict(shared_state.github_jwt_quota)
    saved_it = dict(shared_state.github_quota)
    try:
        shared_state.github_quota["remaining"] = 5000
        shared_state.github_jwt_quota["remaining"] = _JWT_QUOTA_FLOOR - 1
        totals = asyncio.run(refresh_stale_staging_bases())
    finally:
        shared_state.github_jwt_quota.clear()
        shared_state.github_jwt_quota.update(saved_jwt)
        shared_state.github_quota.clear()
        shared_state.github_quota.update(saved_it)

    assert totals["refreshed"] == 0
    assert totals["error"] == 0


def test_service_is_registered_as_a_supervised_background_task():
    """A sweep nobody starts fixes nothing — pin the lifecycle wiring."""
    import inspect

    from app import lifecycle

    source = inspect.getsource(lifecycle)
    assert "from app.staging_base_refresh import staging_base_refresh_service" in source
    assert (
        "staging_base_refresh_task = asyncio.create_task(staging_base_refresh_service())"
        in source
    )
    assert '_bg_tasks["staging_base_refresh"] = staging_base_refresh_task' in source
    assert '"staging_base_refresh": staging_base_refresh_service,' in source
