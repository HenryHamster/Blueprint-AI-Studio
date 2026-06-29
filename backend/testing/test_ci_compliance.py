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
async def test_evaluator_compliant_when_limits_unreadable(mocker):
    """An unreadable limits file is fail-safe: compliant, limits_available=False."""
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

    assert result["compliant"] is True
    assert result["violations"] == []
    assert result["limits_available"] is False


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
    # Branch-literal scan for agent uses known file paths (not tree-based).
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
        new=_fake_tree(_EMPTY_TREE),  # empty tree: no backend .py files to scan
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
        new=_fake_tree(_EMPTY_TREE),
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
    db_session.expire(project)
    refetched = (
        await db_session.execute(select(Project).where(Project.id == project.id))
    ).scalar_one()

    assert ci_compliance.get_ci_compliance(refetched, sha=_HEAD_SHA) == result
    assert ci_compliance.get_ci_compliance(refetched) == {_HEAD_SHA: result}


def test_record_ci_compliance_noop_without_ref():
    """A result with no ``ref`` is a no-op (nothing to key on)."""
    project = Project(name="CI-Compliance No Ref", user_id="test-user-123")
    ci_compliance.record_ci_compliance(project, {"compliant": True, "violations": []})
    assert ci_compliance.get_ci_compliance(project) == {}


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
