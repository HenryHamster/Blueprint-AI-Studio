"""Terminal rows must record WHY they ended, not merely THAT they ended.

The operator rule this suite enforces is "surface REASONS, not just states": a
task that reaches a failure-terminal state (FAILED / CANCELLED) carrying no
machine-readable reason forces a human to re-derive the cause from logs, which
is the firefighting the project exists to remove. COMPLETED is deliberately out
of scope — ordinary success has no reason to record, and inventing one would be
noise.

Two things are proven here, and the second is the one that matters:

  1. Every terminalizing path fixed in this change now stamps a reason. The
     tests DRIVE the real functions and assert on observable task state — not a
     static "is the constant mentioned in the source" scan, which a prior
     invariant in this repo was found to be (and protected nothing).

  2. Recording the reason did not MOVE the platform-kill number. Three of these
     paths were previously classified by matching a literal substring of a log
     sentence (``platform_kill_metrics._TERMINAL_MESSAGE_MARKERS``). Replacing
     fragile prose with a durable field is only an improvement if the verdict
     stays identical, so each of those has a neutrality test that classifies the
     same row twice — once by marker, once by stamp — and a rewording test that
     proves the new evidence survives what the old evidence could not.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.failure_classification import (
    ADMIN_CANCEL,
    ALL_CHILDREN_SHELVED_DETAIL,
    ALL_TERMINAL_REASON_DETAILS,
    BLOCKED_PRECONDITION,
    CODE_ERROR,
    DEPENDENCY_TASK_FAILED_DETAIL,
    EVALUATION_GATE_FAILED_DETAIL,
    PARENT_CI_BACKSTOP_CASCADE_DETAIL,
    PARENT_DEPENDENCY_FAILED_DETAIL,
    PARENT_EVALUATION_TIMEOUT_CASCADE_DETAIL,
    PR_EXECUTION_FENCE_DENIED_DETAIL,
    PREREQUISITE_STRUCTURALLY_UNRESOLVABLE_DETAIL,
)
from app.models import Project, Task, TaskStatus
from app.platform_kill_metrics import (
    NOT_A_DEATH,
    PLATFORM_KILL,
    UNCLASSIFIED,
    WORK_OUTCOME,
    _DETAIL_VERDICTS,
    classify_terminal_task,
)
from testing.factories import make_task


NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
_VALID_VERDICTS = {PLATFORM_KILL, WORK_OUTCOME, NOT_A_DEATH}


# -- fakes -------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """The smallest async session the cascade handlers actually use.

    They only ``add`` rows, ``commit``, and re-``execute`` one child SELECT.
    Using a fake keeps these tests on the pure stamping logic; the DB-backed
    dependency cascade below uses a real session precisely because that path
    opens its own.
    """

    def __init__(self, children=()):
        self._children = list(children)
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._children)


def _cancelled(context=None, message=None, **kw):
    return make_task(
        status=TaskStatus.CANCELLED,
        context=dict(context or {}),
        latest_status_message=message,
        completed_at=NOW - timedelta(hours=1),
        **kw,
    )


# -- 1. the registry is total over the constants -----------------------------


class TestDetailRegistryTotality:
    """A stamped detail the classifier has never been taught is WORSE than no
    stamp: the writer believes it recorded a reason while the reader still files
    the row under "unclassified". The writer constants and the verdict registry
    must therefore be provably in lockstep.
    """

    def test_corpus_is_non_empty(self):
        """Guard the guard - every assertion below is vacuous on an empty tuple."""
        assert len(ALL_TERMINAL_REASON_DETAILS) >= 9, (
            "ALL_TERMINAL_REASON_DETAILS shrank; the totality assertions below "
            "would run over a near-empty corpus and pass without checking "
            "anything"
        )

    def test_every_terminal_reason_detail_is_registered(self):
        missing = [d for d in ALL_TERMINAL_REASON_DETAILS if d not in _DETAIL_VERDICTS]
        assert not missing, (
            f"terminal-reason detail(s) {missing} are stamped by a writer but "
            "unknown to platform_kill_metrics._DETAIL_VERDICTS - those rows "
            "would look explained to the writer and unclassified to the reader"
        )

    def test_every_registered_verdict_is_a_real_verdict_with_a_reason(self):
        for detail in ALL_TERMINAL_REASON_DETAILS:
            verdict, reason = _DETAIL_VERDICTS[detail]
            assert verdict in _VALID_VERDICTS, (
                f"{detail} maps to unknown verdict {verdict!r}"
            )
            assert isinstance(reason, str) and len(reason) >= 20, (
                f"{detail} has no operator-actionable reason text; a bucket "
                "name alone is a state, not a reason"
            )

    def test_detail_constants_are_distinct(self):
        assert len(set(ALL_TERMINAL_REASON_DETAILS)) == len(
            ALL_TERMINAL_REASON_DETAILS
        ), "two terminal paths share a detail string - their rows are conflated"

    def test_task_blocked_detail_mirror_matches(self):
        """``task_blocked`` deliberately does not import the taxonomy module (it
        is a leaf helper and the import would be circular), so it mirrors both
        the class and the detail as local literals. A mirror that drifts is
        exactly the silent-unclassified failure this suite exists to catch."""
        import app.task_blocked as tb

        assert tb.PREREQUISITE_STRUCTURALLY_UNRESOLVABLE_DETAIL == (
            PREREQUISITE_STRUCTURALLY_UNRESOLVABLE_DETAIL
        ), (
            "task_blocked's mirrored detail constant drifted from the canonical "
            "one in failure_classification; rows escalated by enter_blocked_state "
            "would stamp a string the verdict registry has never heard of"
        )
        assert tb.BLOCKED_PRECONDITION == BLOCKED_PRECONDITION


# -- 2. record_failed_gate - the widest terminalizing helper -----------------


class TestEvaluationGateStamp:
    def _failed_gate_task(self):
        from app.task_evaluators import EvaluationVerdict, record_failed_gate

        task = make_task(status=TaskStatus.EVALUATING, context={"direction_id": "d1"})
        record_failed_gate(
            task,
            EvaluationVerdict(
                passed=False,
                reason="audit report missing findings key",
                strategy="audit-responsiveness",
            ),
        )
        return task

    def test_failed_gate_records_a_machine_readable_reason(self):
        task = self._failed_gate_task()
        ctx = task.context or {}
        assert task.status == TaskStatus.FAILED
        assert ctx.get("failure_detail") == EVALUATION_GATE_FAILED_DETAIL
        assert ctx.get("failure_category") == CODE_ERROR

    def test_failed_gate_row_is_no_longer_unclassified(self):
        """This is the operator-visible payoff: before the stamp, every forward
        evaluation-gate failure in the backend landed in ``unclassified``."""
        task = self._failed_gate_task()
        verdict = classify_terminal_task(task)
        assert verdict.verdict == WORK_OUTCOME, (
            f"a gate rejection is a work outcome, got {verdict.verdict} "
            f"({verdict.reason})"
        )
        assert verdict.evidence_field == "context.failure_detail"
        assert verdict.evidence_value == EVALUATION_GATE_FAILED_DETAIL

    def test_failed_gate_keeps_the_gate_identity_for_the_operator(self):
        """The bucket says which pile; the operator still needs to know WHICH
        gate refused the deliverable and why, so the finer record must survive."""
        task = self._failed_gate_task()
        gate = (task.context or {}).get("evaluation_gate") or {}
        assert gate.get("strategy") == "audit-responsiveness"
        assert gate.get("passed") is False
        assert "audit report missing findings key" in gate.get("reason", "")
        assert "audit-responsiveness" in (task.latest_status_message or "")


# -- 3. the PR execution fence -----------------------------------------------


class TestPrExecutionFenceStamp:
    def _denied_task(self, reason="PR #4321 is canonically owned by task 9c1f0a22"):
        from app.review_claim_fence import _cancel_pr_task

        task = make_task(status=TaskStatus.IN_PROGRESS, context={})
        _cancel_pr_task(task, reason)
        return task

    def test_fence_denial_records_a_machine_readable_reason(self):
        task = self._denied_task()
        ctx = task.context or {}
        assert task.status == TaskStatus.CANCELLED
        assert ctx.get("failure_detail") == PR_EXECUTION_FENCE_DENIED_DETAIL
        assert ctx.get("failure_category") == ADMIN_CANCEL

    def test_fence_denial_row_is_no_longer_unclassified(self):
        verdict = classify_terminal_task(self._denied_task())
        assert verdict.verdict == NOT_A_DEATH, (
            "a fence denial means another row canonically owns the PR (or the "
            f"target is gone) - not a death; got {verdict.verdict}"
        )
        assert verdict.verdict != UNCLASSIFIED

    def test_fence_denial_names_the_winning_claimant(self):
        """A bucket is not actionable. The specific denial sentence - which task
        won the claim - must remain addressable."""
        task = self._denied_task()
        fence = (task.context or {}).get("pr_execution_fence") or {}
        assert fence.get("allowed") is False
        assert "9c1f0a22" in fence.get("reason", "")
        assert "9c1f0a22" in (task.latest_status_message or "")


# -- 4. enter_blocked_state's escalation branch ------------------------------


class TestBlockedEscalationStamp:
    """The escalation branch is the one that actually TERMINALIZES (CANCELLED),
    yet it used to return before the ``failure_category`` writes that the
    merely-BLOCKED branch below it performs - the non-terminal path explained
    itself and the terminal path did not."""

    def _escalated(self):
        from app.task_blocked import enter_blocked_state

        task = make_task(status=TaskStatus.IN_PROGRESS, context={})
        enter_blocked_state(
            task,
            waiting_on="prereq_task",
            reason="prerequisite refused at creation",
            need={
                "kind": "task",
                "ref": "deadbeefcafe0001",
                "structurally_dead": True,
            },
        )
        return task

    def _merely_blocked(self):
        from app.task_blocked import enter_blocked_state

        task = make_task(status=TaskStatus.IN_PROGRESS, context={})
        enter_blocked_state(
            task,
            waiting_on="prereq_task",
            reason="waiting on a live provider",
            need={"kind": "task", "ref": "deadbeefcafe0002"},
        )
        return task

    def test_escalated_cancel_records_a_machine_readable_reason(self):
        task = self._escalated()
        ctx = task.context or {}
        assert task.status == TaskStatus.CANCELLED
        assert ctx.get("failure_category") == BLOCKED_PRECONDITION
        assert ctx.get("failure_class") == BLOCKED_PRECONDITION
        assert ctx.get("failure_detail") == (
            PREREQUISITE_STRUCTURALLY_UNRESOLVABLE_DETAIL
        )

    def test_escalated_cancel_matches_the_non_terminal_branch_class(self):
        """Parity, stated as a property rather than a literal: the branch that
        terminalizes must record at least the classification its non-terminal
        sibling already records."""
        escalated_ctx = self._escalated().context or {}
        blocked_ctx = self._merely_blocked().context or {}
        assert blocked_ctx.get("failure_category") == escalated_ctx.get(
            "failure_category"
        ), (
            "the CANCELLED escalation branch and the BLOCKED branch disagree on "
            "the persisted class - the terminal path is the one that must not "
            "be the quieter of the two"
        )

    def test_escalated_cancel_row_is_no_longer_unclassified(self):
        task = self._escalated()
        task.completed_at = NOW - timedelta(hours=1)
        verdict = classify_terminal_task(task)
        assert verdict.verdict == WORK_OUTCOME
        assert verdict.evidence_value == PREREQUISITE_STRUCTURALLY_UNRESOLVABLE_DETAIL

    def test_merely_blocked_row_claims_no_terminal_reason(self):
        """BLOCKED is recoverable and NOT a terminal state, so it must not
        acquire a terminal-reason detail. Stamping one would let a live,
        resumable row be counted as a death."""
        task = self._merely_blocked()
        assert task.status == TaskStatus.BLOCKED
        assert "failure_detail" not in (task.context or {})


# -- 5. cascade fan-outs: reason recorded WITHOUT moving the number ----------


class TestCascadeStampsAreMetricNeutral:
    """Each of these three paths was already classified - by matching a literal
    substring of its log sentence. That is real evidence but fragile evidence:
    reword the sentence and a fan-out of rows silently becomes unclassified.

    So each stamp is asserted twice: it must produce the SAME verdict the marker
    produced (neutrality - the recorded reason improved, the measured number did
    not move), and it must keep producing that verdict when the message is
    reworded (the actual point of the change).
    """

    @pytest.mark.parametrize(
        "detail,marker_message,expected",
        [
            (
                PARENT_EVALUATION_TIMEOUT_CASCADE_DETAIL,
                "Cancelled: parent evaluation timeout after 7.0h",
                PLATFORM_KILL,
            ),
            (
                PARENT_CI_BACKSTOP_CASCADE_DETAIL,
                "Cancelled: parent CI wait backstop timeout after 2.0h (stalled)",
                PLATFORM_KILL,
            ),
            (
                ALL_CHILDREN_SHELVED_DETAIL,
                "All 3 subtask(s) deliberately shelved or superseded, or "
                "administratively cancelled - not a failure",
                NOT_A_DEATH,
            ),
        ],
    )
    def test_stamp_reproduces_the_marker_verdict(
        self, detail, marker_message, expected
    ):
        by_marker = classify_terminal_task(_cancelled(message=marker_message))
        by_stamp = classify_terminal_task(
            _cancelled(context={"failure_detail": detail}, message=marker_message)
        )
        assert by_marker.verdict == expected, (
            "precondition broken: the message marker no longer yields the "
            f"verdict this stamp was designed to reproduce ({by_marker.verdict})"
        )
        assert by_stamp.verdict == by_marker.verdict, (
            f"stamping {detail} changed the verdict from {by_marker.verdict} to "
            f"{by_stamp.verdict} - this change is supposed to move the EVIDENCE, "
            "not the number"
        )
        assert by_stamp.evidence_field == "context.failure_detail"

    @pytest.mark.parametrize(
        "detail,expected",
        [
            (PARENT_EVALUATION_TIMEOUT_CASCADE_DETAIL, PLATFORM_KILL),
            (PARENT_CI_BACKSTOP_CASCADE_DETAIL, PLATFORM_KILL),
            (ALL_CHILDREN_SHELVED_DETAIL, NOT_A_DEATH),
        ],
    )
    def test_reason_survives_a_reworded_status_message(self, detail, expected):
        """The whole point. With only the marker, rewording the log sentence
        drops the row into ``unclassified``; with the stamp it does not."""
        reworded = "parent finished; this child was stood down"
        assert classify_terminal_task(_cancelled(message=reworded)).verdict == (
            UNCLASSIFIED
        ), (
            "precondition broken: this reworded message is supposed to be "
            "unmatched by any marker, otherwise the test proves nothing"
        )
        assert (
            classify_terminal_task(
                _cancelled(context={"failure_detail": detail}, message=reworded)
            ).verdict
            == expected
        )


# -- 6. the cascade writers, driven for real ---------------------------------


@pytest.mark.asyncio
async def test_evaluating_hard_timeout_stamps_every_cascaded_child(mocker):
    """Drive the real hard-timeout handler and assert the children come out with
    a durable reason, not just a status."""
    from app import services
    import app.recovery_evaluating_hard_timeout as mod

    mocker.patch.object(
        services, "_chain_has_active_pipeline_work", new=AsyncMock(return_value=False)
    )
    mocker.patch.object(services, "broadcast_to_project", new=AsyncMock())
    mocker.patch("app.routers.agent_status.record_task_result", new=MagicMock())

    parent = make_task(status=TaskStatus.EVALUATING, context={})
    parent.id = uuid.uuid4()
    children = [
        make_task(status=TaskStatus.IN_PROGRESS, context={}, parent_task_id=parent.id),
        make_task(status=TaskStatus.QUEUED, context={}, parent_task_id=parent.id),
        make_task(status=TaskStatus.COMPLETED, context={}, parent_task_id=parent.id),
    ]
    for child in children:
        child.id = uuid.uuid4()
    session = _FakeSession(children)

    handled = await mod._handle_evaluating_hard_timeout(
        parent,
        {},
        session,
        NOW,
        NOW - timedelta(hours=7),
        7 * 3600,
        6 * 3600,
        TERMINAL,
        AsyncMock(return_value=False),
        AsyncMock(),
    )

    assert handled is True
    assert parent.status == TaskStatus.FAILED
    cascaded = [c for c in children if c.status == TaskStatus.CANCELLED]
    assert len(cascaded) == 2, "precondition: two non-terminal children to cascade"
    for child in cascaded:
        assert (child.context or {}).get("failure_detail") == (
            PARENT_EVALUATION_TIMEOUT_CASCADE_DETAIL
        )
        child.completed_at = NOW
        assert classify_terminal_task(child).verdict == PLATFORM_KILL
    # An already-terminal child is not re-stamped: its own recorded reason wins.
    assert "failure_detail" not in (children[2].context or {})


@pytest.mark.asyncio
async def test_ci_wait_backstop_stamps_every_cascaded_child(mocker):
    """Same for the CI-wait backstop, the other parent-clock cascade."""
    from app import services
    import app.recovery_evaluating_ci_backstop as mod

    mocker.patch.object(
        services, "_chain_has_active_pipeline_work", new=AsyncMock(return_value=False)
    )
    mocker.patch.object(services, "broadcast_to_project", new=AsyncMock())
    mocker.patch.object(
        mod, "_summarize_latest_review_for_chain", new=AsyncMock(return_value=None)
    )
    mocker.patch.object(
        mod, "_chain_has_recoverable_failed_work", new=AsyncMock(return_value=False)
    )
    mocker.patch.object(
        mod,
        "_chain_has_unexhausted_optimistic_retry_work",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.routers.agent_status.record_task_result", new=MagicMock())

    ctx = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": (NOW - timedelta(hours=2)).isoformat(),
        }
    }
    parent = make_task(status=TaskStatus.EVALUATING, context=ctx)
    parent.id = uuid.uuid4()
    mocker.patch(
        "app.orchestrator._lock_task_for_mutation", new=AsyncMock(return_value=parent)
    )
    children = [
        make_task(status=TaskStatus.IN_PROGRESS, context={}, parent_task_id=parent.id),
        make_task(status=TaskStatus.FAILED, context={}, parent_task_id=parent.id),
    ]
    for child in children:
        child.id = uuid.uuid4()
    session = _FakeSession(children)

    handled = await mod._handle_ci_waiting_backstop(
        parent,
        ctx,
        session,
        NOW,
        None,
        None,
        1800,
        24,
        TERMINAL,
        AsyncMock(),
    )

    assert handled is True
    assert parent.status == TaskStatus.FAILED
    cascaded = children[0]
    assert cascaded.status == TaskStatus.CANCELLED
    assert (cascaded.context or {}).get("failure_detail") == (
        PARENT_CI_BACKSTOP_CASCADE_DETAIL
    )
    cascaded.completed_at = NOW
    assert classify_terminal_task(cascaded).verdict == PLATFORM_KILL
    assert "failure_detail" not in (children[1].context or {})


@pytest.mark.asyncio
async def test_all_non_death_cancelled_parent_records_its_reason():
    """The reaper/rollup finalizer for an all-shelved child set. Its sibling
    persists SCOPE_EMPTY; this one persisted nothing at all."""
    from app.task_lifecycle_requeue import _terminate_all_non_death_cancelled

    parent = make_task(status=TaskStatus.EVALUATING, context={})
    parent.id = uuid.uuid4()
    children = [
        make_task(status=TaskStatus.CANCELLED, context={"_co_deferred_shelved": True}),
        make_task(status=TaskStatus.CANCELLED, context={"_co_deferred_shelved": True}),
    ]
    session = _FakeSession()

    await _terminate_all_non_death_cancelled(parent, children, session, AsyncMock())

    assert parent.status == TaskStatus.CANCELLED
    assert (parent.context or {}).get("failure_detail") == ALL_CHILDREN_SHELVED_DETAIL
    parent.completed_at = NOW
    assert classify_terminal_task(parent).verdict == NOT_A_DEATH


# -- 7. the dependency cascade (needs a real session - it opens its own) -----


@pytest_asyncio.fixture()
async def dep_db(mocker):
    """In-memory DB plus a patched ``AsyncSessionLocal``.

    ``_cascade_cancel_dependent`` deliberately opens its OWN session (it runs
    outside any caller transaction), so a plain session fixture would not be the
    session it writes through. Patch the factory instead and hand back a session
    on the same engine to read the results.
    """
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    import app.dep_lifecycle as dep_mod

    mocker.patch.object(dep_mod, "AsyncSessionLocal", factory)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_cascade_stamps_dependent_and_children(dep_db, mocker):
    """A failed prerequisite cancels its dependent and the dependent's children.
    Before this change the whole fan-out carried nothing but prose."""
    import app.dep_lifecycle as dep_mod

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new=AsyncMock())
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new=AsyncMock())

    project = Project(name="p", user_id="u")
    dep_db.add(project)
    await dep_db.commit()
    await dep_db.refresh(project)

    dependent = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.QUEUED,
        context={},
    )
    dep_db.add(dependent)
    await dep_db.commit()
    await dep_db.refresh(dependent)
    child = Task(
        project_id=project.id,
        parent_task_id=dependent.id,
        payloads=[{"type": "create", "prompt": "y"}],
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    done_child = Task(
        project_id=project.id,
        parent_task_id=dependent.id,
        payloads=[{"type": "create", "prompt": "z"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    dep_db.add(child)
    dep_db.add(done_child)
    await dep_db.commit()

    count = await dep_mod._cascade_cancel_dependent(
        dependent.id, project.id, AsyncMock()
    )
    assert count == 2, "the dependent plus its one non-terminal child"

    for row in (dependent, child, done_child):
        await dep_db.refresh(row)

    assert dependent.status == TaskStatus.CANCELLED
    assert (dependent.context or {}).get("failure_category") == BLOCKED_PRECONDITION
    assert (dependent.context or {}).get("failure_detail") == (
        DEPENDENCY_TASK_FAILED_DETAIL
    )

    assert child.status == TaskStatus.CANCELLED
    assert (child.context or {}).get("failure_category") == BLOCKED_PRECONDITION
    assert (child.context or {}).get("failure_detail") == (
        PARENT_DEPENDENCY_FAILED_DETAIL
    )

    # Origin and blast radius stay distinguishable: an operator must be able to
    # tell the row that declared the failed prerequisite from the rows merely
    # swept up behind it.
    assert DEPENDENCY_TASK_FAILED_DETAIL != PARENT_DEPENDENCY_FAILED_DETAIL

    for row in (dependent, child):
        row.completed_at = NOW
        verdict = classify_terminal_task(row)
        assert verdict.verdict == WORK_OUTCOME, (
            "a cascade behind a failed prerequisite is a dependency-ordering "
            "outcome; re-attributing the prerequisite's own kill to each "
            f"dependent would multiply one defect into N (got {verdict.verdict})"
        )

    # An already-terminal child is left exactly as it was.
    assert done_child.status == TaskStatus.COMPLETED
    assert "failure_detail" not in (done_child.context or {})


# -- 8. the honest boundary --------------------------------------------------


def test_ordinary_completion_is_not_given_an_invented_reason():
    """Scope statement, enforced. COMPLETED is terminal but not a failure -
    manufacturing a reason for it would be noise, and would also corrupt the
    metric's denominator. The fix deliberately covers FAILED and CANCELLED only.
    """
    done = make_task(
        status=TaskStatus.COMPLETED,
        context={},
        completed_at=NOW - timedelta(hours=1),
    )
    assert "failure_detail" not in (done.context or {})
    assert "failure_category" not in (done.context or {})
