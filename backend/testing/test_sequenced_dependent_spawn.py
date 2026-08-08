"""Sequenced dependent spawn — the campaign build-on-top primitive.

Covers the two declarative payload fields that make "task B builds ON TOP OF
task A's merged state" expressible:

  * ``require_deps_landed_on`` — deterministic dispatch gate: hold until every
    ``depends_on`` dependency is COMPLETED and its merged work has LANDED on
    the named branch, bypassing the genuine-code-dependency file-overlap
    heuristic (under which an ordering-only cross-base dep is non-blocking).
  * ``release_base_branch`` — release-time base repoint: when the dependency
    gate opens, the task's ``base_branch`` is rewritten to the named branch at
    the ``ensure_dependent_base_recut`` chokepoint, so the agent clones that
    branch's tip — a starting state that includes the dependencies' merges.
"""

import pytest
from pydantic import ValidationError

from app.failure_classification import SCOPE_EMPTY
from app.models import TaskStatus
from app.orchestrator_health import (
    _check_candidate_task_deps,
    ensure_dependent_base_recut,
)
from app.payload_schemas import BasePayload
from testing.factories import make_project, make_task


async def _noop_verify(_task, _session):
    return True


async def _persist(db_session, *objs):
    for obj in objs:
        db_session.add(obj)
    await db_session.commit()
    for obj in objs:
        await db_session.refresh(obj)


class TestSequencedPayloadFields:
    """Schema validation for the two new BasePayload / CreateTaskPayload fields."""

    def test_base_payload_accepts_valid_branches(self):
        p = BasePayload(
            require_deps_landed_on="main",
            release_base_branch="campaign/checkout-flow",
        )
        assert p.require_deps_landed_on == "main"
        assert p.release_base_branch == "campaign/checkout-flow"

    @pytest.mark.parametrize("bad", ["-lead-dash", "a..b", "x/.hidden", " "])
    def test_base_payload_rejects_unsafe_branch_names(self, bad):
        with pytest.raises(ValidationError):
            BasePayload(require_deps_landed_on=bad)
        with pytest.raises(ValidationError):
            BasePayload(release_base_branch=bad)

    def test_create_task_payload_carries_fields(self):
        from app.admin import CreateTaskPayload

        p = CreateTaskPayload(
            github_repo="owner/repo",
            title="t",
            prompt="p",
            require_deps_landed_on="main",
            release_base_branch="campaign/x",
        )
        assert p.require_deps_landed_on == "main"
        assert p.release_base_branch == "campaign/x"
        with pytest.raises(ValidationError):
            CreateTaskPayload(
                github_repo="owner/repo",
                title="t",
                prompt="p",
                require_deps_landed_on="a..b",
            )


class TestRequireDepsLandedOnGate:
    """_check_candidate_task_deps holds declared-sequence candidates until every
    dependency has landed on the named branch."""

    @pytest.mark.asyncio
    async def test_holds_ordering_only_cross_base_dep_until_landed(self, db_session):
        """An ordering-only cross-base dep is non-blocking under the heuristic;
        the declared gate must hold it until the dep LANDS on the named branch."""
        project = make_project(name="Seq Gate", user_id="u1")
        await _persist(db_session, project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "A", "base_branch": "plan/other"}],
            status=TaskStatus.COMPLETED,
        )
        await _persist(db_session, dep)

        candidate = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B builds on A",
                    "base_branch": "main",
                    "require_deps_landed_on": "main",
                }
            ],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        await _persist(db_session, candidate)

        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "waiting", (
            "COMPLETED-but-not-landed dep must hold a declared-sequence "
            f"candidate, got {result!r}"
        )

        # Once the dep lands on integration, the gate opens.
        dep.context = {
            **(dep.context or {}),
            "pr_merged": True,
            "landed_on_integration": True,
        }
        await _persist(db_session, dep)
        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "eligible", f"landed dep should release the gate, got {result!r}"

    @pytest.mark.asyncio
    async def test_holds_while_dep_in_flight(self, db_session):
        """An in-flight ordering-only cross-base dep is non-blocking under the
        heuristic; the declared gate must hold regardless."""
        project = make_project(name="Seq Inflight", user_id="u1")
        await _persist(db_session, project)
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "A", "base_branch": "plan/other"}],
            status=TaskStatus.IN_PROGRESS,
        )
        await _persist(db_session, dep)
        candidate = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B",
                    "base_branch": "main",
                    "require_deps_landed_on": "main",
                }
            ],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        await _persist(db_session, candidate)

        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "waiting", f"in-flight dep must hold, got {result!r}"

    @pytest.mark.asyncio
    async def test_non_integration_branch_landing(self, db_session):
        """A gate naming a campaign/staging branch is satisfied by a PR that
        merged into that branch (resolve_pr_landing_branch), not by merges
        elsewhere."""
        project = make_project(name="Seq Campaign Branch", user_id="u1")
        await _persist(db_session, project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "A", "base_branch": "campaign/x"}],
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                "_non_merge_unblock_reason": "staging_merge",
                "review_loop": {"base_branch": "plan/elsewhere"},
            },
        )
        await _persist(db_session, dep)
        candidate = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B",
                    "base_branch": "campaign/x",
                    "require_deps_landed_on": "campaign/x",
                }
            ],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        await _persist(db_session, candidate)

        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "waiting", (
            f"merge onto a DIFFERENT branch must not satisfy the gate, got {result!r}"
        )

        dep.context = {
            **(dep.context or {}),
            "review_loop": {"base_branch": "campaign/x"},
        }
        await _persist(db_session, dep)
        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "eligible", (
            f"merge onto the named branch should satisfy the gate, got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_scope_empty_dep_does_not_wedge_sequence(self, db_session):
        """A clean SCOPE_EMPTY cancellation stays a satisfied, non-blocking dep
        even under the declared gate — it must not wedge the campaign."""
        project = make_project(name="Seq ScopeEmpty", user_id="u1")
        await _persist(db_session, project)
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "A", "base_branch": "main"}],
            status=TaskStatus.CANCELLED,
            context={"failure_category": SCOPE_EMPTY},
        )
        await _persist(db_session, dep)
        candidate = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B",
                    "base_branch": "main",
                    "require_deps_landed_on": "main",
                }
            ],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        await _persist(db_session, candidate)

        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "eligible", (
            f"SCOPE_EMPTY dep must stay satisfied under the gate, got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_already_resolved_noop_dep_does_not_wedge_sequence(self, db_session):
        """A no-op completion that affirmed its change already exists on HEAD
        (suppresses_redispatch) never lands anywhere — it must satisfy the gate
        rather than hold the sequence forever (the COMPLETED-non-landed
        permanent-wedge class)."""
        project = make_project(name="Seq NoOp", user_id="u1")
        await _persist(db_session, project)
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "A", "base_branch": "main"}],
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                "landed_on_integration": False,
                "_non_merge_unblock_reason": "no_op_completion",
                "no_op_reason": "already_done",
            },
        )
        await _persist(db_session, dep)
        candidate = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B",
                    "base_branch": "main",
                    "require_deps_landed_on": "main",
                }
            ],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        await _persist(db_session, candidate)

        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "eligible", (
            f"already-resolved no-op dep must not wedge the sequence, got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_undeclared_candidate_keeps_heuristic_behavior(self, db_session):
        """Without require_deps_landed_on, a COMPLETED ordering-only cross-base
        dep stays non-blocking — the pre-existing heuristic path is unchanged."""
        project = make_project(name="Seq Heuristic", user_id="u1")
        await _persist(db_session, project)
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "A", "base_branch": "plan/other"}],
            status=TaskStatus.COMPLETED,
        )
        await _persist(db_session, dep)
        candidate = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "B", "base_branch": "main"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        await _persist(db_session, candidate)

        result, _ = await _check_candidate_task_deps(
            candidate, [dep], [dep.id], db_session, _noop_verify
        )
        assert result == "eligible", (
            f"undeclared ordering-only cross-base dep must stay non-blocking, got {result!r}"
        )


class TestReleaseBaseRepoint:
    """ensure_dependent_base_recut repoints base_branch to the declared
    release_base_branch at the dependency-release chokepoint."""

    @pytest.mark.asyncio
    async def test_repoints_base_at_release(self, db_session):
        project = make_project(name="Repoint", user_id="u1")
        await _persist(db_session, project)
        dependent = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B",
                    "base_branch": "plan/abc",
                    "release_base_branch": "main",
                }
            ],
            status=TaskStatus.QUEUED,
        )
        await _persist(db_session, dependent)

        ok = await ensure_dependent_base_recut(dependent, [], db_session)
        assert ok is True
        assert dependent.first_payload["base_branch"] == "main"
        # No recut accounting was armed — the repoint must not path through the
        # recut failure/backoff machinery.
        assert not any(k.startswith("_recut_") for k in (dependent.context or {}))

    @pytest.mark.asyncio
    async def test_repoint_keeps_landed_dep_recut_a_noop(self, db_session):
        """With a landed genuine code-dep, the recut source is the integration
        branch; after the repoint the base IS that branch, so the recut
        short-circuits 'ok' without a GitHub merge call and never arms the
        recut backoff (a GitHub call would fail here and record _recut_*)."""
        project = make_project(name="Repoint Noop", user_id="u1")
        await _persist(db_session, project)
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "A", "base_branch": "plan/other"}],
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                "landed_on_integration": True,
                "related_files": ["app/shared.py"],
            },
        )
        await _persist(db_session, dep)
        dependent = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B",
                    "base_branch": "plan/abc",
                    "release_base_branch": "main",
                    "require_deps_landed_on": "main",
                }
            ],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
            context={"related_files": ["app/shared.py"]},
        )
        await _persist(db_session, dependent)

        ok = await ensure_dependent_base_recut(dependent, [dep], db_session)
        assert ok is True
        assert dependent.first_payload["base_branch"] == "main"
        assert not any(k.startswith("_recut_") for k in (dependent.context or {}))

    @pytest.mark.asyncio
    async def test_repoint_noop_when_already_on_release_base(self, db_session):
        project = make_project(name="Repoint Idem", user_id="u1")
        await _persist(db_session, project)
        dependent = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "prompt": "B",
                    "base_branch": "main",
                    "release_base_branch": "main",
                }
            ],
            status=TaskStatus.QUEUED,
        )
        await _persist(db_session, dependent)

        ok = await ensure_dependent_base_recut(dependent, [], db_session)
        assert ok is True
        assert dependent.first_payload["base_branch"] == "main"

    @pytest.mark.asyncio
    async def test_no_repoint_without_declaration(self, db_session):
        project = make_project(name="Repoint Absent", user_id="u1")
        await _persist(db_session, project)
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "B", "base_branch": "plan/abc"}],
            status=TaskStatus.QUEUED,
        )
        await _persist(db_session, dependent)

        ok = await ensure_dependent_base_recut(dependent, [], db_session)
        assert ok is True
        assert dependent.first_payload["base_branch"] == "plan/abc"
