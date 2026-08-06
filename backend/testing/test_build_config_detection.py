"""Tests for build_config_detection cooldown-stamp ordering.

Verifies that maybe_refresh_empty_build_config and
maybe_redetect_on_emptiness_disagreement stamp their cooldowns AFTER a
successful API call, not before — so a transient failure (no token, GitHub
502) does not consume the 6-hour window and block recovery.

Also covers the stale-``repo_is_empty=False`` self-heal trigger wiring: a
``clone_failed`` report whose text matches the REPO_UNINITIALIZED signature
(a genuinely commitless repo) is routed to
``maybe_correct_stale_commitless_repo_signal`` through the real
``update_agent_status`` handler, correcting the flag once GitHub confirms the
repo has no commits — so the next task takes the empty-repo path instead of
looping clone_failed forever.
"""

from __future__ import annotations

import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Project, Task, TaskStatus
from app.routers.agent_status import update_agent_status
from app.schemas import AgentStatusPayload


# ---------------------------------------------------------------------------
# Cross-test isolation
# ---------------------------------------------------------------------------

# Attributes the tests in this file assign directly onto sys.modules entries.
_PATCHED_MODULE_ATTRS = (
    ("app.github", "get_repo_installation_token"),
    ("app.shared_state", "get_project_integration_branch"),
    ("app.routers.admin_onboard", "_detect_build_config"),
)

# Every sys.modules key _install_stub_modules may create and _reload_bcd may
# swap. Restored wholesale so neither a leftover stub (installed when the real
# module happened not to be imported yet) nor the reloaded
# build_config_detection object outlives the test that made it.
_TOUCHED_MODULES = (
    "app.github",
    "app.shared_state",
    "app.routers",
    "app.routers.admin_onboard",
    "app.schemas",
    "app.build_config_detection",
    "pydantic",
    "sqlalchemy",
    "sqlalchemy.orm",
    "sqlalchemy.orm.attributes",
)

_MISSING = object()


@pytest.fixture(autouse=True)
def _restore_directly_patched_modules():
    """Undo this file's direct ``sys.modules[...].attr = mock`` assignments.

    ``_install_stub_modules`` registers its stubs with ``sys.modules.setdefault``,
    which is a NO-OP once the real modules are already imported — i.e. in every
    full-suite run. The tests below then assign ``AsyncMock``s onto whatever is
    in ``sys.modules``, which in that case is the REAL ``app.github`` /
    ``app.shared_state`` / ``app.routers.admin_onboard``, and nothing ever puts
    them back.

    That leaked a ``get_repo_installation_token`` returning ``"tok"`` into the
    rest of the session, so every later test that mints a GitHub token got
    ``"tok"`` instead of a real mint (``assert 'tok' == 'ghs_mint_1'`` in
    test_github_token_resolver), and a ``_detect_build_config`` stuck on this
    file's canned payload. Snapshot-and-restore keeps the assignments working
    exactly as written while confining them to the test that makes them.
    """
    saved_attrs = [
        (sys.modules[mod_name], attr, getattr(sys.modules[mod_name], attr, _MISSING))
        for mod_name, attr in _PATCHED_MODULE_ATTRS
        if mod_name in sys.modules
    ]
    saved_modules = [
        (name, sys.modules.get(name, _MISSING)) for name in _TOUCHED_MODULES
    ]

    yield

    for mod, attr, original in saved_attrs:
        if original is _MISSING:
            if hasattr(mod, attr):
                delattr(mod, attr)
        else:
            setattr(mod, attr, original)
    for name, original in saved_modules:
        if original is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(
    project_id="proj-1",
    github_owner="owner",
    github_repo="repo",
    build_config=None,
):
    p = MagicMock()
    p.id = project_id
    p.github_owner = github_owner
    p.github_repo = github_repo
    p.build_config = build_config if build_config is not None else {}
    return p


def _make_session():
    s = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    return s


# Stub out the heavy lazy imports so build_config_detection can be loaded
# and the success paths can be exercised without sqlmodel/database deps.
def _install_stub_modules():
    _STUBS = {}

    # app.github
    github_mod = types.ModuleType("app.github")
    github_mod.get_repo_installation_token = AsyncMock(return_value="fake-token")
    _STUBS["app.github"] = github_mod

    # app.shared_state
    shared_state_mod = types.ModuleType("app.shared_state")
    shared_state_mod.get_project_integration_branch = MagicMock(return_value="main")
    _STUBS["app.shared_state"] = shared_state_mod

    # app.routers.admin_onboard
    admin_onboard_mod = types.ModuleType("app.routers.admin_onboard")
    admin_onboard_mod._detect_build_config = AsyncMock(return_value={})
    admin_onboard_mod.GREENFIELD_SOURCE_FILE_THRESHOLD = 5
    _STUBS["app.routers"] = types.ModuleType("app.routers")
    _STUBS["app.routers.admin_onboard"] = admin_onboard_mod

    # app.schemas
    schemas_mod = types.ModuleType("app.schemas")

    class _FakeBuildConfig:
        def __init__(self, **kw):
            self._d = kw

        def model_dump(self, **_kw):
            return self._d

    schemas_mod.BuildConfig = _FakeBuildConfig
    _STUBS["app.schemas"] = schemas_mod

    # pydantic (for ValidationError in the function body)
    try:
        import pydantic as _pydantic  # noqa: PLC0415
        del _pydantic
    except ImportError:
        pydantic_mod = types.ModuleType("pydantic")
        pydantic_mod.ValidationError = Exception
        _STUBS["pydantic"] = pydantic_mod

    # sqlalchemy.orm.attributes
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415, F401
    except ImportError:
        sa_orm_mod = types.ModuleType("sqlalchemy.orm.attributes")
        sa_orm_mod.flag_modified = MagicMock()
        _STUBS["sqlalchemy"] = types.ModuleType("sqlalchemy")
        _STUBS["sqlalchemy.orm"] = types.ModuleType("sqlalchemy.orm")
        _STUBS["sqlalchemy.orm.attributes"] = sa_orm_mod

    for name, mod in _STUBS.items():
        sys.modules.setdefault(name, mod)

    return _STUBS


def _reload_bcd():
    """Reload build_config_detection to get fresh (empty) cooldown dicts."""
    _install_stub_modules()
    mod_name = "app.build_config_detection"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import app.build_config_detection as m  # noqa: PLC0415
    return m


# ---------------------------------------------------------------------------
# maybe_refresh_empty_build_config — cooldown stamp ordering
# ---------------------------------------------------------------------------

class TestMaybeRefreshEmptyBuildConfigCooldown:
    """Cooldown must be stamped AFTER a successful API call, not before."""

    @pytest.mark.asyncio
    async def test_cooldown_not_stamped_on_token_failure(self):
        """If get_repo_installation_token returns None, cooldown must NOT be stamped."""
        mod = _reload_bcd()
        project = _make_project()
        session = _make_session()

        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value=None)
        result = await mod.maybe_refresh_empty_build_config(project, session)

        assert result is False
        # Cooldown must be empty — a transient failure must not block retries.
        assert str(project.id) not in mod._redetect_empty_cooldown

    @pytest.mark.asyncio
    async def test_cooldown_stamped_after_success(self):
        """Cooldown must be set after a successful detection + commit."""
        mod = _reload_bcd()
        project = _make_project(build_config={"repo_is_empty": True})
        session = _make_session()
        detected = {"repo_is_empty": False, "source_file_count": 3, "build_command": "npm test"}

        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value="tok")
        sys.modules["app.routers.admin_onboard"]._detect_build_config = AsyncMock(return_value=detected)
        sys.modules["app.shared_state"].get_project_integration_branch = MagicMock(return_value="main")

        result = await mod.maybe_refresh_empty_build_config(project, session)

        assert result is True
        assert str(project.id) in mod._redetect_empty_cooldown

    @pytest.mark.asyncio
    async def test_cooldown_retry_allowed_after_token_failure(self):
        """After a token failure, a second attempt must proceed (cooldown not consumed)."""
        mod = _reload_bcd()
        session = _make_session()
        detected = {"repo_is_empty": False, "source_file_count": 5, "build_command": "make"}

        # First call — token fails
        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value=None)
        project2 = _make_project(project_id="proj-retry", build_config={"repo_is_empty": True})
        first = await mod.maybe_refresh_empty_build_config(project2, session)

        assert first is False
        assert str(project2.id) not in mod._redetect_empty_cooldown

        # Second call — token succeeds; must not be blocked by cooldown
        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value="tok")
        sys.modules["app.routers.admin_onboard"]._detect_build_config = AsyncMock(return_value=detected)
        sys.modules["app.shared_state"].get_project_integration_branch = MagicMock(return_value="main")
        second = await mod.maybe_refresh_empty_build_config(project2, session)

        assert second is True

    @pytest.mark.asyncio
    async def test_cooldown_gates_second_call_after_success(self):
        """After a successful call, a second call within the window returns False."""
        mod = _reload_bcd()
        project = _make_project(build_config={"repo_is_empty": True})
        session = _make_session()
        detected = {"repo_is_empty": False, "source_file_count": 5, "build_command": "make"}

        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value="tok")
        sys.modules["app.routers.admin_onboard"]._detect_build_config = AsyncMock(return_value=detected)
        sys.modules["app.shared_state"].get_project_integration_branch = MagicMock(return_value="main")

        first = await mod.maybe_refresh_empty_build_config(project, session)
        second = await mod.maybe_refresh_empty_build_config(project, session)

        assert first is True
        assert second is False  # within cooldown window


# ---------------------------------------------------------------------------
# maybe_redetect_on_emptiness_disagreement — cooldown stamp ordering
# ---------------------------------------------------------------------------

class TestMaybeRedetectOnEmptinessDisagreementCooldown:
    """Cooldown must be stamped AFTER a successful API call, not before."""

    @pytest.mark.asyncio
    async def test_cooldown_not_stamped_on_token_failure(self):
        """Token failure must NOT consume the cooldown window."""
        mod = _reload_bcd()
        project = _make_project(build_config={"repo_is_empty": False})
        session = _make_session()

        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value=None)
        result = await mod.maybe_redetect_on_emptiness_disagreement(
            project, session, observed_empty=True
        )

        assert result is False
        assert str(project.id) not in mod._redetect_disagreement_cooldown

    @pytest.mark.asyncio
    async def test_cooldown_stamped_after_success(self):
        """Cooldown must be set only after a successful commit."""
        mod = _reload_bcd()
        project = _make_project(build_config={"repo_is_empty": False})
        session = _make_session()
        detected = {"repo_is_empty": True, "source_file_count": 0}

        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value="tok")
        sys.modules["app.routers.admin_onboard"]._detect_build_config = AsyncMock(return_value=detected)
        sys.modules["app.shared_state"].get_project_integration_branch = MagicMock(return_value="main")

        result = await mod.maybe_redetect_on_emptiness_disagreement(
            project, session, observed_empty=True
        )

        assert result is True
        assert str(project.id) in mod._redetect_disagreement_cooldown

    @pytest.mark.asyncio
    async def test_cooldown_retry_after_token_failure(self):
        """A second attempt after a failed first must be allowed (cooldown not consumed)."""
        mod = _reload_bcd()
        project = _make_project(build_config={"repo_is_empty": False})
        session = _make_session()
        detected = {"repo_is_empty": True, "source_file_count": 0}

        # First call — token fails
        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value=None)
        first = await mod.maybe_redetect_on_emptiness_disagreement(
            project, session, observed_empty=True
        )

        assert first is False
        assert str(project.id) not in mod._redetect_disagreement_cooldown

        # Second call — token succeeds; must not be blocked by cooldown
        sys.modules["app.github"].get_repo_installation_token = AsyncMock(return_value="tok")
        sys.modules["app.routers.admin_onboard"]._detect_build_config = AsyncMock(return_value=detected)
        sys.modules["app.shared_state"].get_project_integration_branch = MagicMock(return_value="main")
        second = await mod.maybe_redetect_on_emptiness_disagreement(
            project, session, observed_empty=True
        )

        assert second is True


# ---------------------------------------------------------------------------
# Stale-``repo_is_empty=False`` self-heal: clone_failed + REPO_UNINITIALIZED
# ---------------------------------------------------------------------------
# Regression for the commitless-repo clone_failed loop. A repo with NO branches
# at all carrying a stale durable repo_is_empty=False never reaches
# verifyCloneDelivered (git clone --branch <trunk> dies first), so its failure
# text matches the REPO_UNINITIALIZED patterns, not the clone-delivery-
# disagreement string. The update_agent_status handler must route that shape to
# maybe_correct_stale_commitless_repo_signal, which re-verifies against GitHub
# (repo_has_commits) and flips the flag once the repo is confirmed commitless.
# The clone-delivery-disagreement shape is covered alongside so the original
# trigger is not regressed.


def _heal_project(name="commitless-external", repo="commitless-external"):
    return Project(
        name=name,
        github_owner="HenryHamster",
        github_repo=repo,
        build_config={"repo_is_empty": False, "source_file_count": 5},
    )


def _heal_task(task_id, project_id, vm_id):
    return Task(
        id=task_id,
        project_id=project_id,
        title="clone a commitless repo",
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=vm_id,
        context={},
        created_by="ORCHESTRATOR",
        attempt_count=0,
    )


def _heal_session_mock(task, project):
    """Minimal AsyncSession mock for update_agent_status: the FOR UPDATE query
    returns the task; session.get() returns the project for Project and None
    for everything else."""
    task_row = MagicMock()
    task_row.scalars.return_value.first.return_value = task

    session = AsyncMock()
    session.execute = AsyncMock(return_value=task_row)
    session.get = AsyncMock(
        side_effect=lambda cls, pk: project if cls is Project else None
    )
    return session


def _heal_request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/agent/status",
            "headers": [],
        }
    )


def _heal_side_effect_patches():
    """Patches the handler's unrelated side-effects so the self-heal path is the
    only live machinery (mirrors the cross-seam tests in test_empty_repo_onboarding)."""
    return (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
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
    )


async def _run_heal_report(project, *, logs, repo_has_commits_value=_MISSING, extra_patches=()):
    """Drive one FAILED clone_failed report through the real update_agent_status
    handler. The self-heal GitHub oracle (repo_has_commits) is only configured
    when a value is given; extra_patches are entered for shape-specific needs
    (e.g. the disagreement path's _detect_build_config)."""
    from contextlib import ExitStack

    from fastapi import BackgroundTasks

    from app.build_config_detection import (
        _redetect_stale_commitless_cooldown,
        _redetect_stale_empty_cooldown,
    )

    _redetect_stale_commitless_cooldown.clear()
    _redetect_stale_empty_cooldown.clear()

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    vm_id = "vm-heal-commitless"

    task = _heal_task(task_id, project_id, vm_id)
    session = _heal_session_mock(task, project)
    project.id = project_id

    payload = AgentStatusPayload(
        task_id=task_id,
        status=TaskStatus.FAILED,
        failure_category="clone_failed",
        logs=logs,
    )
    token = {"sub": str(task_id), "vm_instance_id": vm_id}

    with ExitStack() as stack:
        for _p in _heal_side_effect_patches():
            stack.enter_context(_p)
        if repo_has_commits_value is not _MISSING:
            stack.enter_context(
                patch(
                    "app.github_refs.repo_has_commits",
                    new_callable=AsyncMock,
                    return_value=repo_has_commits_value,
                )
            )
        for _p in extra_patches:
            stack.enter_context(_p)
        return await update_agent_status(
            request=_heal_request(),
            payload=payload,
            background_tasks=BackgroundTasks(),
            session=session,
            token=token,
        )


class TestAgentStatusRepoUninitializedSelfHeal:
    """A clone_failed report with a REPO_UNINITIALIZED signature self-heals the
    stale repo_is_empty=False through the real handler, and fail-loud is kept
    when GitHub confirms the repo actually has commits."""

    @pytest.mark.asyncio
    async def test_repo_uninitialized_signature_corrects_stale_flag(self):
        """Commitless repo (repo_has_commits=False) → repo_is_empty flips to True
        so the next attempt takes the empty-repo path instead of looping."""
        project = _heal_project()

        await _run_heal_report(
            project,
            logs=(
                "Clone failed: the repository has no commits, so its default "
                "branch does not exist yet. Blueprint seeds an initial commit "
                "when a project is onboarded — this repo was never seeded."
            ),
            repo_has_commits_value=False,
        )

        assert project.build_config["repo_is_empty"] is True, (
            "REPO_UNINITIALIZED clone_failed must self-heal the stale "
            f"repo_is_empty=False once GitHub confirms no commits; got "
            f"build_config={project.build_config!r}"
        )
        assert project.build_config.get("source_file_count") == 0
        assert project.build_config.get("has_ci") is False

    @pytest.mark.asyncio
    async def test_repo_uninitialized_signature_keeps_fail_loud_when_repo_has_commits(
        self,
    ):
        """A repo that actually has commits must NOT have its flag flipped — a
        genuine clone shortfall keeps failing loud."""
        project = _heal_project(name="real-content-repo", repo="real-content-repo")

        await _run_heal_report(
            project,
            logs="fatal: repository has no commits",
            repo_has_commits_value=True,
        )

        assert project.build_config["repo_is_empty"] is False, (
            "A repo with real content must NOT be corrected to empty; got "
            f"build_config={project.build_config!r}"
        )

    @pytest.mark.asyncio
    async def test_clone_delivery_disagreement_signature_still_triggers(self):
        """The ORIGINAL trigger (durable says non-empty, cloned tree is empty)
        still routes to the disagreement self-heal and corrects the flag."""
        project = _heal_project(name="disagreement-repo", repo="disagreement-repo")

        await _run_heal_report(
            project,
            logs=(
                "clone_failed: the onboarding scan counted 5 source file(s), "
                "but the cloned tree has only 0 source file(s) under the same "
                "exclusion rules — the clone did not deliver the expected content"
            ),
            extra_patches=(
                patch(
                    "app.routers.admin_onboard._detect_build_config",
                    new_callable=AsyncMock,
                    return_value={"repo_is_empty": True, "source_file_count": 0},
                ),
                patch(
                    "app.shared_state.get_project_integration_branch",
                    new_callable=MagicMock,
                    return_value="main",
                ),
            ),
        )

        assert project.build_config["repo_is_empty"] is True, (
            "The clone-delivery-disagreement trigger must still self-heal; got "
            f"build_config={project.build_config!r}"
        )

    @pytest.mark.asyncio
    async def test_clone_succeeds_no_head_commitless_signature_corrects_stale_flag(
        self,
    ):
        """The clone-SUCCEEDS commitless path (git clone exits 0, verifyCloneDelivered
        finds no HEAD, probes the remote, and throws the canonical
        repository-has-no-commits signature) must ALSO route to the commitless
        self-heal and flip the stale repo_is_empty=False once GitHub confirms the
        repo has no commits — otherwise this shape loops clone_failed forever the
        same way the clone-fails branch-missing shape did before PR #16725."""
        project = _heal_project(name="clone-succeeds-empty", repo="clone-succeeds-empty")

        await _run_heal_report(
            project,
            logs=(
                "clone_failed: the repository has no commits, so the clone produced "
                "an empty working tree with no HEAD. Blueprint seeds an initial "
                "commit when a project is onboarded — this repo was never seeded."
            ),
            repo_has_commits_value=False,
        )

        assert project.build_config["repo_is_empty"] is True, (
            "The clone-succeeds commitless signature must self-heal the stale "
            f"repo_is_empty=False; got build_config={project.build_config!r}"
        )
        assert project.build_config.get("source_file_count") == 0
