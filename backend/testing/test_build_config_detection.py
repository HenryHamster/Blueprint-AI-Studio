"""Tests for build_config_detection cooldown-stamp ordering.

Verifies that maybe_refresh_empty_build_config and
maybe_redetect_on_emptiness_disagreement stamp their cooldowns AFTER a
successful API call, not before — so a transient failure (no token, GitHub
502) does not consume the 6-hour window and block recovery.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


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
