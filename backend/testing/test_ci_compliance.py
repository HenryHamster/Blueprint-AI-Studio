"""Tests for the per-merge CI-compliance evaluator + per-SHA store (Direction E).

Exercises :mod:`app.ci_compliance`:

  * the at-SHA evaluator flags a module-size breach with the correct
    ``{rel, count, cap}`` and ``compliant=False``, reading EVERY file through the
    mocked GitHub file-read-at-SHA path (never the working tree);
  * a within-cap HEAD yields ``compliant=True``;
  * a file that 404s at the ref is skipped, never crashing the evaluation;
  * uncapped source files over the coverage threshold are reported as violations;
  * hardcoded branch literals in backend/agent source are reported as violations;
  * an unavailable repo tree degrades gracefully (tree_available=False);
  * ``record_ci_compliance`` / ``get_ci_compliance`` round-trip a result keyed by
    its commit SHA against the real ``Project.ci_compliance`` JSONB column.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import select

from app import ci_compliance
from app.models import Project

# A capped backend module guaranteed to exist in module_size_limits.json with a
# small cap, so an over-cap body is easy to synthesise.
_CAPPED_REL = "task_completion_self_audit.py"
_CAP = 600  # kept in sync with module_size_limits.json backend section
_HEAD_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


def _limits_json(cap: int = _CAP) -> str:
    """A minimal module_size_limits.json body with a single capped backend key."""
    return json.dumps({"backend": {_CAPPED_REL: cap}})


# A capped backend/testing/ file guaranteed to be exercised once the evaluator
# scans the enforced ``testing`` section (mirroring CI's TestTestFileSizeGuardrails).
_TESTING_REL = "test_fake_module.py"
_TESTING_CAP = 100


def _limits_json_with_testing(
    cap: int = _CAP,
    testing_cap: int = _TESTING_CAP,
    testing_rel: str = _TESTING_REL,
) -> str:
    """A limits JSON with capped backend + testing sections."""
    return json.dumps(
        {
            "backend": {_CAPPED_REL: cap},
            "testing": {testing_rel: testing_cap},
        }
    )


def _fake_reader(file_bodies: dict[str, str | None]):
    """Build an async stand-in for ``github.get_file_text_at_ref``.

    ``file_bodies`` maps a repo-relative path to the text returned at the ref;
    a missing key (or an explicit ``None``) models a 404 / unreadable file.
    """

    async def _reader(path, *, token, owner, repo, ref, project_id=None):
        return file_bodies.get(path)

    return _reader


def _fake_tree(entries: list[dict] | None):
    """Build an async stand-in for ``github.list_repo_tree_paths_at_ref``.

    ``entries`` is the list of ``{path, size}`` dicts to return; ``None``
    models a tree-unavailable (truncated / error) condition.
    """

    async def _lister(token, owner, repo, ref, *, project_id=None):
        return entries

    return _lister


# Minimal empty tree: no source files in it — used for tests that don't need
# the uncapped / branch-literal scans to fire.
_EMPTY_TREE: list[dict] = []


@pytest.mark.asyncio
async def test_evaluator_flags_over_cap_violation(mocker):
    """An over-cap capped module is reported as a violation; compliant=False."""
    over_cap_body = "\n".join("x" for _ in range(_CAP + 5))  # _CAP+5 lines
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": over_cap_body,
    }
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(_EMPTY_TREE),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["ref"] == _HEAD_SHA
    assert result["compliant"] is False
    assert result["limits_available"] is True
    assert result["violations"] == [
        {
            "section": "backend",
            "rel": _CAPPED_REL,
            "count": _CAP + 5,
            "cap": _CAP,
        }
    ]
    assert result["branch_literal_violations"] == []


@pytest.mark.asyncio
async def test_evaluator_compliant_when_within_cap(mocker):
    """A within-cap HEAD yields compliant=True and no violations."""
    within_cap_body = "\n".join("x" for _ in range(_CAP - 100))
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": within_cap_body,
    }
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(_EMPTY_TREE),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is True
    assert result["violations"] == []
    assert result["branch_literal_violations"] == []
    assert result["checked"] == 1


@pytest.mark.asyncio
async def test_evaluator_skips_unreadable_file(mocker):
    """A capped file that 404s at the ref is skipped, never a violation/crash."""
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        # capped file path intentionally ABSENT -> reader returns None (404)
    }
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(_EMPTY_TREE),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is True
    assert result["violations"] == []
    assert result["checked"] == 0


@pytest.mark.asyncio
async def test_unreadable_limits_fails_closed(mocker):
    """An unreadable limits config fails closed: compliant=False + diagnostic.

    The fail-open behavior this replaces reported compliant=True when the
    size-limits file could not be read — the guard silently stopped guarding
    and reported success. Fail-closed discipline: when the limits source is
    unreadable, the gate must refuse with a diagnostic naming the path it tried
    to read.
    """
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader({}),  # everything 404s, including the limits file
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(_EMPTY_TREE),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    assert result["violations"] == []
    assert result["limits_available"] is False
    assert result["limits_status"] == "unreadable"
    assert ci_compliance._MODULE_SIZE_LIMITS_PATH in result["limits_error"]
    assert result["tree_available"] is True
    assert result["checked"] == 0
    assert result["branch_literal_violations"] == []


@pytest.mark.asyncio
async def test_evaluator_fails_closed_when_limits_malformed(mocker):
    """A malformed limits config (bad JSON / non-object) fails closed too."""
    for bad_body in ("not json", "[1, 2, 3]"):
        bodies = {ci_compliance._MODULE_SIZE_LIMITS_PATH: bad_body}
        mocker.patch.object(
            ci_compliance.github,
            "get_file_text_at_ref",
            new=_fake_reader(bodies),
        )
        mocker.patch.object(
            ci_compliance.github,
            "list_repo_tree_paths_at_ref",
            new=_fake_tree(_EMPTY_TREE),
        )

        result = await ci_compliance.evaluate_ci_compliance_at_ref(
            token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
        )

        assert result["compliant"] is False, f"body {bad_body!r} must fail closed"
        assert result["violations"] == []
        assert result["limits_available"] is False
        assert result["limits_status"] == "malformed"
        assert ci_compliance._MODULE_SIZE_LIMITS_PATH in result["limits_error"]
        assert result["branch_literal_violations"] == []


@pytest.mark.asyncio
async def test_evaluator_compliant_when_limits_legitimately_empty(mocker):
    """A valid config declaring no limits ({}) still passes.

    Distinguishes 'unreadable' from 'legitimately empty': an empty-but-valid
    caps object means the size guardrails have nothing to enforce, and the
    branch-literal scan (independent of the limits file) still runs.
    """
    bodies = {ci_compliance._MODULE_SIZE_LIMITS_PATH: "{}"}
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(_EMPTY_TREE),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is True
    assert result["violations"] == []
    assert result["limits_available"] is True
    assert result["limits_status"] == "ok"
    assert result["limits_error"] is None
    assert result["checked"] == 0
    assert result["branch_literal_violations"] == []


@pytest.mark.asyncio
async def test_evaluator_flags_testing_section_breach(mocker):
    """A capped backend/testing/ file over its cap is a section-'testing' violation."""
    testing_body = "\n".join("x" for _ in range(_TESTING_CAP + 5))
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json_with_testing(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 100)),
        f"backend/testing/{_TESTING_REL}": testing_body,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github, "list_repo_tree_paths_at_ref", new=_fake_tree(_EMPTY_TREE)
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    assert result["limits_available"] is True
    testing_violations = [
        v for v in result["violations"] if v.get("section") == "testing"
    ]
    assert len(testing_violations) == 1
    assert testing_violations[0] == {
        "section": "testing",
        "rel": _TESTING_REL,
        "count": _TESTING_CAP + 5,
        "cap": _TESTING_CAP,
    }


@pytest.mark.asyncio
async def test_evaluator_flags_testing_uncapped_large_file(mocker):
    """An uncapped backend/testing/*.py over the threshold is an uncapped violation."""
    big_body = "\n".join("x" for _ in range(ci_compliance._UNCAPPED_THRESHOLD + 10))
    big_body_bytes = len(big_body.encode())
    testing_rel = "test_large_module.py"
    testing_path = f"backend/testing/{testing_rel}"

    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json_with_testing(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 100)),
        testing_path: big_body,
    }
    tree = [
        {"path": testing_path, "size": big_body_bytes},
        {"path": f"backend/app/{_CAPPED_REL}", "size": 10000},
    ]
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github, "list_repo_tree_paths_at_ref", new=_fake_tree(tree)
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    uncapped_violations = [v for v in result["violations"] if v.get("cap") is None]
    assert len(uncapped_violations) == 1
    assert uncapped_violations[0]["section"] == "testing"
    assert uncapped_violations[0]["rel"] == testing_rel
    assert uncapped_violations[0]["count"] == ci_compliance._UNCAPPED_THRESHOLD + 10


@pytest.mark.asyncio
async def test_evaluator_runs_branch_literal_scan_when_limits_unreadable(mocker):
    """Unreadable limits still surface branch-literal violations from the tree.

    The limits file 404s (not in ``bodies``), so the size scans are skipped and
    ``limits_available`` is False — but the branch-literal scan depends only on
    the repo tree (as CI's branch-literal invariants do), so an offending
    backend/app/ file must still be reported even while the verdict fails
    closed on the unreadable limits.
    """
    offending_body = (
        "def get_branch(config):\n"
        "    return config.get('integration_branch') else 'main'\n"
    )
    tree = [{"path": "backend/app/some_module.py", "size": 100}]
    bodies = {
        # limits file intentionally ABSENT -> unreadable
        "backend/app/some_module.py": offending_body,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github, "list_repo_tree_paths_at_ref", new=_fake_tree(tree)
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    assert result["violations"] == []
    assert result["checked"] == 0
    assert result["limits_available"] is False
    assert result["tree_available"] is True
    assert len(result["branch_literal_violations"]) >= 1
    offender = result["branch_literal_violations"][0]
    assert offender["section"] == "backend"
    assert offender["rel"] == "some_module.py"


@pytest.mark.asyncio
async def test_evaluator_flags_uncapped_large_file(mocker):
    """A source file over the threshold with no cap entry is reported as a violation."""
    # The uncapped file must be large enough to pass the byte-size pre-filter
    # (_scan_uncapped_files skips files whose tree-reported size is under 30000 bytes).
    big_body = "\n".join("x" for _ in range(ci_compliance._UNCAPPED_THRESHOLD + 10))
    big_body_bytes = len(big_body.encode())
    uncapped_rel = "new_large_module.py"
    uncapped_path = f"backend/app/{uncapped_rel}"

    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        uncapped_path: big_body,
    }
    tree = [
        {"path": uncapped_path, "size": big_body_bytes},
        {"path": f"backend/app/{_CAPPED_REL}", "size": 10000},
    ]
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(tree),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    uncapped_violations = [v for v in result["violations"] if v.get("cap") is None]
    assert len(uncapped_violations) == 1
    assert uncapped_violations[0]["section"] == "backend"
    assert uncapped_violations[0]["rel"] == uncapped_rel
    assert uncapped_violations[0]["count"] == ci_compliance._UNCAPPED_THRESHOLD + 10


@pytest.mark.asyncio
async def test_evaluator_does_not_flag_capped_file_as_uncapped(mocker):
    """A file already capped in module_size_limits.json is not flagged as uncapped."""
    big_body = "\n".join("x" for _ in range(ci_compliance._UNCAPPED_THRESHOLD + 10))
    big_body_bytes = len(big_body.encode())
    capped_path = f"backend/app/{_CAPPED_REL}"

    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        capped_path: big_body,
    }
    tree = [{"path": capped_path, "size": big_body_bytes}]
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(tree),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    # The file is in caps so it's only evaluated as a capped breach, not as uncapped.
    uncapped_violations = [v for v in result["violations"] if v.get("cap") is None]
    assert uncapped_violations == [], "capped file must not appear in uncapped violations"
    # It SHOULD appear as a capped violation (over the small test cap).
    capped_violations = [v for v in result["violations"] if v.get("cap") is not None]
    assert len(capped_violations) == 1


@pytest.mark.asyncio
async def test_evaluator_flags_backend_branch_literal(mocker):
    """A backend/app/ .py file with a hardcoded branch fallback is a violation."""
    offending_body = (
        "def get_branch(config):\n"
        "    return config.get('integration_branch') else 'main'\n"
    )
    tree = [{"path": "backend/app/some_module.py", "size": 100}]
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        "backend/app/some_module.py": offending_body,
    }
    # Also need to stub the agent files so they don't 404-crash (they return None, which is fine).
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(tree),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    assert len(result["branch_literal_violations"]) >= 1
    offender = result["branch_literal_violations"][0]
    assert offender["section"] == "backend"
    assert offender["rel"] == "some_module.py"
    assert "line" in offender
    assert "text" in offender


@pytest.mark.asyncio
async def test_evaluator_flags_agent_branch_literal(mocker):
    """An agent handler file with a bare branch literal comparison is a violation."""
    offending_ts = (
        "function handleReview(baseBranch: string) {\n"
        "  if (baseBranch === 'main') { return; }\n"
        "}\n"
    )
    agent_path = "agent/src/handlers/review-fix.ts"
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: offending_ts,
    }
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        # Tree-based agent scan requires the file to appear in the repo tree.
        new=_fake_tree([{"path": agent_path, "size": 120}]),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    agent_violations = [
        v for v in result["branch_literal_violations"] if v.get("section") == "agent"
    ]
    assert len(agent_violations) >= 1
    assert agent_violations[0]["rel"] == "handlers/review-fix.ts"


@pytest.mark.asyncio
async def test_evaluator_allows_paren_fallback_in_agent(mocker):
    """An agent file using the allowed ``(promotionTarget ?? 'main')`` pattern is clean."""
    clean_ts = (
        "function handleReview(baseBranch: string, promotionTarget: string) {\n"
        "  if (baseBranch === (promotionTarget ?? 'main')) { return; }\n"
        "}\n"
    )
    agent_path = "agent/src/handlers/review-fix.ts"
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: clean_ts,
    }
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        # Tree-based agent scan: file must appear in tree. Paren fallback should
        # be stripped before pattern matching so the guard does not fire.
        new=_fake_tree([{"path": agent_path, "size": 100}]),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    agent_violations = [
        v for v in result["branch_literal_violations"] if v.get("section") == "agent"
    ]
    assert agent_violations == [], "parenthesized fallback must not trigger the guard"


@pytest.mark.asyncio
async def test_evaluator_degrades_when_tree_unavailable(mocker):
    """When the repo tree is unavailable, uncapped/branch-literal scans are skipped."""
    within_cap_body = "\n".join("x" for _ in range(_CAP - 100))
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": within_cap_body,
    }
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(bodies),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree(None),  # None = tree unavailable
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["tree_available"] is False
    assert result["compliant"] is True  # capped file is within cap
    assert result["violations"] == []
    assert result["branch_literal_violations"] == []


@pytest.mark.asyncio
async def test_record_and_get_round_trip_keyed_by_sha(db_session):
    """record_ci_compliance / get_ci_compliance persist+read per SHA via JSONB."""
    project = Project(name="CI-Compliance Round Trip", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    result = {
        "ref": _HEAD_SHA,
        "compliant": False,
        "violations": [
            {"section": "backend", "rel": _CAPPED_REL, "count": _CAP + 5, "cap": _CAP}
        ],
        "branch_literal_violations": [],
        "checked": 1,
        "limits_available": True,
        "tree_available": True,
    }

    # Empty before any record.
    assert ci_compliance.get_ci_compliance(project) == {}
    assert ci_compliance.get_ci_compliance(project, sha=_HEAD_SHA) is None

    ci_compliance.record_ci_compliance(project, result)
    await db_session.commit()

    # Round-trip through the real JSONB column: re-fetch from the DB.
    # Snapshot the id BEFORE expire(): reading an expired attribute emits a
    # sync lazy-load that raises MissingGreenlet under the asyncpg harness.
    project_id = project.id
    db_session.expire(project)
    refetched = (
        await db_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()

    assert ci_compliance.get_ci_compliance(refetched, sha=_HEAD_SHA) == result
    assert ci_compliance.get_ci_compliance(refetched) == {_HEAD_SHA: result}


def test_record_ci_compliance_noop_without_ref():
    """A result with no ``ref`` is a no-op (nothing to key on)."""
    project = Project(name="CI-Compliance No Ref", user_id="test-user-123")
    ci_compliance.record_ci_compliance(project, {"compliant": True, "violations": []})
    assert ci_compliance.get_ci_compliance(project) == {}


def test_ci_compliance_branch_literals_use_shared_scanner():
    """ci_compliance sources branch-literal patterns from branch_literal_compliance.

    Asserts that the local duplicate constant names no longer exist and that
    ci_compliance imports pattern/allowlist objects from the shared module.
    This is the acceptance gate for the dedup refactor — both production-code
    and test changes must be present for this test to be meaningful.
    """
    from app import branch_literal_compliance

    # Local duplicate names must not exist in ci_compliance.
    assert not hasattr(ci_compliance, "_BACKEND_BRANCH_LITERAL_PATTERNS"), (
        "local duplicate _BACKEND_BRANCH_LITERAL_PATTERNS must be removed"
    )
    assert not hasattr(ci_compliance, "_BACKEND_BRANCH_LITERAL_ALLOW"), (
        "local duplicate _BACKEND_BRANCH_LITERAL_ALLOW must be removed"
    )
    assert not hasattr(ci_compliance, "_AGENT_FORBIDDEN_RE"), (
        "local duplicate _AGENT_FORBIDDEN_RE must be removed"
    )
    assert not hasattr(ci_compliance, "_AGENT_PAREN_FALLBACK_RE"), (
        "local duplicate _AGENT_PAREN_FALLBACK_RE must be removed"
    )
    assert not hasattr(ci_compliance, "_AGENT_BRANCH_LITERAL_REL_PATHS"), (
        "local duplicate _AGENT_BRANCH_LITERAL_REL_PATHS must be removed"
    )

    # ci_compliance must reference the shared module's objects by identity.
    assert ci_compliance.BACKEND_PATTERNS is branch_literal_compliance.BACKEND_PATTERNS
    assert ci_compliance.SURFACE_CONFIGS is branch_literal_compliance.SURFACE_CONFIGS
    assert ci_compliance.AGENT_SOURCE_FILES is branch_literal_compliance.AGENT_SOURCE_FILES
    assert ci_compliance.CI_AGENT_FORBIDDEN_RE is branch_literal_compliance.CI_AGENT_FORBIDDEN_RE
    assert (
        ci_compliance.CI_AGENT_PAREN_FALLBACK_RE
        is branch_literal_compliance.CI_AGENT_PAREN_FALLBACK_RE
    )
    assert (
        ci_compliance.strip_typescript_with_linenos
        is branch_literal_compliance.strip_typescript_with_linenos
    )


def test_record_ci_compliance_bounds_history():
    """The per-SHA map is bounded to _MAX_RECORDED_SHAS, evicting oldest."""
    project = Project(name="CI-Compliance Bound", user_id="test-user-123")
    total = ci_compliance._MAX_RECORDED_SHAS + 10
    for i in range(total):
        ci_compliance.record_ci_compliance(
            project, {"ref": f"sha{i:04d}", "compliant": True, "violations": []}
        )
    stored = ci_compliance.get_ci_compliance(project)
    assert len(stored) == ci_compliance._MAX_RECORDED_SHAS
    # Oldest evicted, newest retained.
    assert "sha0000" not in stored
    assert f"sha{total - 1:04d}" in stored


@pytest.mark.asyncio
async def test_evaluator_flags_backend_display_string_branch_literal(mocker):
    """display-main-range pattern in backend/app/ triggers a branch-literal violation."""
    offending_body = (
        "def release_title(base, head):\n"
        "    title = f'Merge main...{head}'\n"
        "    return title\n"
    )
    tree = [{"path": "backend/app/release_mod.py", "size": 100}]
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        "backend/app/release_mod.py": offending_body,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github, "list_repo_tree_paths_at_ref", new=_fake_tree(tree)
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    backend_lit = [
        v for v in result["branch_literal_violations"] if v["section"] == "backend"
    ]
    assert len(backend_lit) >= 1
    assert backend_lit[0]["rel"] == "release_mod.py"
    assert "main..." in backend_lit[0]["text"]


@pytest.mark.asyncio
async def test_evaluator_flags_agent_assign_branch_literal(mocker):
    """Agent assign-branch pattern (branch = 'main') triggers a violation."""
    offending_ts = (
        "function checkout(opts) {\n"
        "  const branch = 'main';\n"
        "  return branch;\n"
        "}\n"
    )
    agent_path = "agent/src/handlers/standard.ts"
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: offending_ts,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree([{"path": agent_path, "size": 90}]),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    agent_violations = [
        v for v in result["branch_literal_violations"] if v["section"] == "agent"
    ]
    assert len(agent_violations) >= 1
    assert agent_violations[0]["rel"] == "handlers/standard.ts"


@pytest.mark.asyncio
async def test_evaluator_flags_agent_object_branch_literal(mocker):
    """Agent object-baseBranch pattern (baseBranch: 'main') triggers a violation."""
    offending_ts = (
        "function buildPayload(ref: string) {\n"
        "  return { baseBranch: 'main', ref };\n"
        "}\n"
    )
    agent_path = "agent/src/handlers/specialized.ts"
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: offending_ts,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree([{"path": agent_path, "size": 90}]),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    agent_violations = [
        v for v in result["branch_literal_violations"] if v["section"] == "agent"
    ]
    assert len(agent_violations) >= 1
    assert agent_violations[0]["rel"] == "handlers/specialized.ts"


@pytest.mark.asyncio
async def test_evaluator_flags_agent_bare_fallback_literal(mocker):
    """A bare ``?? 'main'`` (not inside parens) in agent source is a violation."""
    offending_ts = (
        "function getBase(opts: any) {\n"
        "  const base = opts.base ?? 'main';\n"
        "  return base;\n"
        "}\n"
    )
    agent_path = "agent/src/handlers/code-change.ts"
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: offending_ts,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree([{"path": agent_path, "size": 90}]),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    agent_violations = [
        v for v in result["branch_literal_violations"] if v["section"] == "agent"
    ]
    assert len(agent_violations) >= 1
    assert agent_violations[0]["rel"] == "handlers/code-change.ts"


@pytest.mark.asyncio
async def test_evaluator_scans_non_listed_agent_ts_file(mocker):
    """Tree-based agent scan catches violations in files outside the old fixed-path list."""
    offending_ts = (
        "function helper(branch: string) {\n"
        "  if (branch === 'main') { return true; }\n"
        "}\n"
    )
    # This file was NOT in the old _AGENT_BRANCH_LITERAL_REL_PATHS fixed list.
    agent_path = "agent/src/helpers/branch-utils.ts"
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: offending_ts,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree([{"path": agent_path, "size": 90}]),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    assert result["compliant"] is False
    agent_violations = [
        v for v in result["branch_literal_violations"] if v["section"] == "agent"
    ]
    assert len(agent_violations) >= 1
    assert agent_violations[0]["rel"] == "helpers/branch-utils.ts"


async def _scan_agent_ts(mocker, agent_path: str, offending_ts: str):
    """Run the evaluator over a single agent .ts file, returning agent violations."""
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: offending_ts,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree([{"path": agent_path, "size": 90}]),
    )
    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )
    return [
        v for v in result["branch_literal_violations"] if v["section"] == "agent"
    ]


@pytest.mark.asyncio
async def test_supplemental_loose_equality_main(mocker):
    """Loose-equality (``== 'main'``) — not covered by canonical ===/!== — is flagged.

    The canonical AGENT_PATTERNS['equality-main'] only matches ===/!==, so this
    supplemental-only pattern is the sole reason this line is caught.
    """
    offending_ts = (
        "function helper(branch: string) {\n"
        "  if (branch == 'main') { return true; }\n"
        "}\n"
    )
    agent_path = "agent/src/helpers/loose-eq.ts"
    agent_violations = await _scan_agent_ts(mocker, agent_path, offending_ts)
    assert len(agent_violations) == 1
    assert agent_violations[0]["rel"] == "helpers/loose-eq.ts"
    assert agent_violations[0]["line"] == 2


@pytest.mark.asyncio
async def test_supplemental_equality_master(mocker):
    """Equality against 'master' (any operator) — supplemental-only — is flagged."""
    offending_ts = (
        "function helper(branch: string) {\n"
        "  if (branch === 'master') { return true; }\n"
        "}\n"
    )
    agent_path = "agent/src/helpers/master-eq.ts"
    agent_violations = await _scan_agent_ts(mocker, agent_path, offending_ts)
    assert len(agent_violations) == 1
    assert agent_violations[0]["rel"] == "helpers/master-eq.ts"
    assert agent_violations[0]["line"] == 2


@pytest.mark.asyncio
async def test_strict_equality_main_not_double_counted(mocker):
    """``=== 'main'`` is flagged exactly once — the canonical pattern, not doubled.

    Guards the regression where 'equality-loose-main' (``==``) matched the ``==``
    substring inside ``===``, producing two violations for one offending line. The
    anchored regex must NOT fire inside ===/!==.
    """
    offending_ts = (
        "function helper(branch: string) {\n"
        "  if (branch === 'main') { return true; }\n"
        "}\n"
    )
    agent_path = "agent/src/helpers/strict-eq.ts"
    agent_violations = await _scan_agent_ts(mocker, agent_path, offending_ts)
    assert len(agent_violations) == 1
    assert agent_violations[0]["rel"] == "helpers/strict-eq.ts"
    assert agent_violations[0]["line"] == 2


@pytest.mark.asyncio
async def test_strict_inequality_main_not_double_counted(mocker):
    """``!== 'main'`` is flagged exactly once — the loose ``!=`` regex must not fire."""
    offending_ts = (
        "function helper(branch: string) {\n"
        "  if (branch !== 'main') { return true; }\n"
        "}\n"
    )
    agent_path = "agent/src/helpers/strict-neq.ts"
    agent_violations = await _scan_agent_ts(mocker, agent_path, offending_ts)
    assert len(agent_violations) == 1
    assert agent_violations[0]["rel"] == "helpers/strict-neq.ts"
    assert agent_violations[0]["line"] == 2


@pytest.mark.asyncio
async def test_supplemental_pattern_respects_allowlist(mocker):
    """An allowlist entry for a supplemental pattern suppresses its violation.

    Mirrors the canonical AGENT_PATTERNS allowlist behaviour: a
    path::line::pattern entry in SURFACE_CONFIGS['agent'].allowlist must
    suppress a supplemental ('equality-loose-main') match too.
    """
    import dataclasses

    from app.branch_literal_compliance import (
        SURFACE_CONFIGS,
        BranchLiteralAllowEntry,
    )

    offending_ts = (
        "function helper(branch: string) {\n"
        "  if (branch == 'main') { return true; }\n"
        "}\n"
    )
    agent_path = "agent/src/helpers/allowlisted.ts"
    allow_entry = BranchLiteralAllowEntry(
        path="helpers/allowlisted.ts",
        line=2,
        pattern="equality-loose-main",
        rationale="test allowlist of supplemental pattern",
    )
    patched_agent = dataclasses.replace(
        SURFACE_CONFIGS["agent"], allowlist=(allow_entry,)
    )
    mocker.patch.dict(
        ci_compliance.SURFACE_CONFIGS, {"agent": patched_agent}, clear=False
    )

    agent_violations = await _scan_agent_ts(mocker, agent_path, offending_ts)
    assert agent_violations == []


@pytest.mark.asyncio
async def test_evaluator_allows_agent_allowlisted_entry(mocker):
    """Violations at allowlisted path+line+pattern in SURFACE_CONFIGS are suppressed."""
    from app.branch_literal_compliance import SURFACE_CONFIGS

    agent_config = SURFACE_CONFIGS["agent"]
    # Pick the first allowlist entry as the test target.
    allow_entry = agent_config.allowlist[0]
    # Build a source with the flagged pattern on the exact allowlisted line.
    lines = [""] * allow_entry.line
    lines[allow_entry.line - 1] = "  const b = opts.base ?? 'main';"
    offending_ts = "\n".join(lines)

    agent_path = f"agent/src/{allow_entry.path}"
    bodies = {
        ci_compliance._MODULE_SIZE_LIMITS_PATH: _limits_json(),
        f"backend/app/{_CAPPED_REL}": "\n".join("x" for _ in range(_CAP - 50)),
        agent_path: offending_ts,
    }
    mocker.patch.object(
        ci_compliance.github, "get_file_text_at_ref", new=_fake_reader(bodies)
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=_fake_tree([{"path": agent_path, "size": len(offending_ts)}]),
    )

    result = await ci_compliance.evaluate_ci_compliance_at_ref(
        token="t", owner="o", repo="r", ref=_HEAD_SHA, project_id="p"
    )

    agent_violations = [
        v
        for v in result["branch_literal_violations"]
        if v["section"] == "agent" and v["line"] == allow_entry.line
    ]
    assert agent_violations == [], (
        f"allowlisted entry at {allow_entry.path}:{allow_entry.line} must not trigger a violation"
    )


def test_in_flight_evals_dict_exists_and_is_mutable():
    """_in_flight_evals is a module-level dict used for in-flight coalescing.

    This is the acceptance gate for the coalescing implementation: the dict
    must exist and be mutable so concurrent callers can register and look up
    in-flight tasks by ``"{project_id}:{sha}"`` key.
    """
    assert hasattr(ci_compliance, "_in_flight_evals"), (
        "_in_flight_evals process-level registry is missing from ci_compliance"
    )
    d = ci_compliance._in_flight_evals
    assert isinstance(d, dict), "_in_flight_evals must be a dict"
    # Confirm it is writable (the coalescing logic reads and writes it).
    original_len = len(d)
    d["_test_key"] = None  # type: ignore[assignment]
    assert "_test_key" in d
    del d["_test_key"]
    assert len(d) == original_len
