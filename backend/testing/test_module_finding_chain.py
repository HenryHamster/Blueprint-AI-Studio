from __future__ import annotations

import uuid
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import ModuleFindingChain, Project, ProjectMessage, Task, TaskStatus
from app.module_finding_chain import (
    CHAIN_ESCALATION_THRESHOLD,
    _guard_chain_key,
    elect_guard_suppression_owners,
    extract_guard_suppression_identities,
    find_and_mark_chain_escalation,
    ingest_guard_suppression_receipt,
    record_merged_pr_finding_chains,
    resolve_landed_guard_suppression_remediation,
    resolved_review_findings,
)
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
)
from app.review_loop_convergence import build_cross_episode_abandon_candidate
from app.time_utils import _utcnow


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self):
        self.rows: list[ModuleFindingChain] = []
        self.added_tasks: list[Task] = []
        self.messages: list[ProjectMessage] = []

    async def execute(self, _statement):
        if "projectmessage" in str(_statement).lower():
            return _Result(self.messages)
        return _Result(self.rows)

    def begin_nested(self):
        return _Nested()

    @property
    def no_autoflush(self):
        return nullcontext()

    async def flush(self, _objects=None):
        return None

    def add(self, value):
        if isinstance(value, ModuleFindingChain) and value not in self.rows:
            self.rows.append(value)
        elif isinstance(value, Task):
            self.added_tasks.append(value)


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def _context(description: str, issue_id: str) -> dict:
    issue = {
        "issue_id": issue_id,
        "severity": "major",
        "description": description,
        "file": "backend/app/example.py",
    }
    return {
        "_pr_changed_files": ["backend/app/example.py"],
        "_pr_changed_files_github_verified": True,
        "review_loop": {
            "review_history": [
                {"issues": [issue], "resolutions": [{"issue_id": issue_id, "disposition": "fixed"}]}
            ],
            "issue_ledger": {
                "resolved": [{"issue_id": issue_id, "disposition": "fixed"}]
            },
        },
    }


def test_resolved_review_findings_excludes_open_concerns():
    context = _context("same concern", "fixed")
    context["review_loop"]["issue_ledger"]["resolved"] = []
    context["review_loop"]["review_history"][0]["resolutions"] = []
    assert resolved_review_findings(context) == []


@pytest.mark.asyncio
async def test_three_distinct_merged_prs_escalate_once():
    project_id = uuid.uuid4()
    session = _Session()
    descriptions = [
        "The session cleanup ownership is incorrect and leaks resources",
        "Session cleanup ownership remains incorrect and leaks resources",
        "Incorrect session cleanup ownership still leaks resources",
    ]
    for pr_number, description in enumerate(descriptions, start=101):
        task = Task(project_id=project_id, context=_context(description, f"issue-{pr_number}"))
        await record_merged_pr_finding_chains(task, session, pr_number=pr_number)

    assert len(session.rows) == 1
    chain = session.rows[0]
    assert chain.occurrences == 3
    assert chain.verified_occurrences == 3
    assert chain.pr_numbers == [101, 102, 103]

    issue = {
        "severity": "major",
        "description": "Session cleanup ownership leaks resources",
        "file": "backend/app/example.py",
    }
    candidates = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue],
        authoritative_files=[{"filename": "backend/app/example.py"}],
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["status"] == "chain_escalation"
    assert candidate["occurrences"] == CHAIN_ESCALATION_THRESHOLD

    # Re-entry on the same PR without a new merged occurrence: the chain is
    # still past threshold, so the detector finds it again. It is not a
    # lifetime suppressor — dedup for repeated re-entry on one PR happens
    # downstream, in the alert layer (alert_dispatch, keyed by
    # (task_id, title) where the title encodes the concern and occurrence
    # count, not the PR number).
    same_pr_candidates = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue],
        authoritative_files=[{"filename": "backend/app/example.py"}],
    )
    assert len(same_pr_candidates) == 1
    assert same_pr_candidates[0]["finding_key"] == candidate["finding_key"]

    # A NEW distinct merged PR re-raising the same concern must still be
    # detectable as a fresh escalation candidate -- the chain must not go
    # permanently silent after its first escalation.
    fourth_task = Task(
        project_id=project_id,
        context=_context(
            "The session cleanup ownership remains incorrect and leaks resources",
            "issue-104",
        ),
    )
    await record_merged_pr_finding_chains(fourth_task, session, pr_number=104)
    assert chain.occurrences == 4
    assert chain.verified_occurrences == 4

    next_pr_candidates = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue],
        authoritative_files=[{"filename": "backend/app/example.py"}],
    )
    assert len(next_pr_candidates) == 1
    next_pr_candidate = next_pr_candidates[0]
    assert next_pr_candidate["finding_key"] == candidate["finding_key"]
    assert next_pr_candidate["occurrences"] == 4


def _context_for_file(description: str, issue_id: str, file_path: str) -> dict:
    issue = {
        "issue_id": issue_id,
        "severity": "major",
        "description": description,
        "file": file_path,
    }
    return {
        "_pr_changed_files": [file_path],
        "_pr_changed_files_github_verified": True,
        "review_loop": {
            "review_history": [
                {"issues": [issue], "resolutions": [{"issue_id": issue_id, "disposition": "fixed"}]}
            ],
            "issue_ledger": {
                "resolved": [{"issue_id": issue_id, "disposition": "fixed"}]
            },
        },
    }


@pytest.mark.asyncio
async def test_two_distinct_chains_past_threshold_on_one_pr_both_surface():
    """A single PR can raise two unrelated concerns, each independently past
    threshold in a different module. Both must be returned, and the result
    must not depend on the order the issues appear in."""
    project_id = uuid.uuid4()
    session = _Session()

    one_descriptions = [
        "The retry loop lacks backoff and hammers the api",
        "Retry loop lacks backoff and still hammers the api",
        "Retry loop still lacks backoff and hammers the api",
    ]
    two_descriptions = [
        "The cache key omits tenant id and leaks data",
        "Cache key omits tenant id and still leaks data",
        "Cache key still omits tenant id and leaks data",
    ]
    for pr_number, (desc_one, desc_two) in enumerate(
        zip(one_descriptions, two_descriptions), start=401
    ):
        task_one = Task(
            project_id=project_id,
            context=_context_for_file(desc_one, f"one-{pr_number}", "backend/app/one.py"),
        )
        await record_merged_pr_finding_chains(task_one, session, pr_number=pr_number)
        task_two = Task(
            project_id=project_id,
            context=_context_for_file(desc_two, f"two-{pr_number}", "backend/app/two.py"),
        )
        await record_merged_pr_finding_chains(task_two, session, pr_number=pr_number)

    assert len(session.rows) == 2

    issue_one = {
        "severity": "major",
        "description": "Retry loop lacks backoff and hammers the api",
        "file": "backend/app/one.py",
    }
    issue_two = {
        "severity": "major",
        "description": "Cache key omits tenant id and leaks data",
        "file": "backend/app/two.py",
    }
    authoritative_files = [
        {"filename": "backend/app/one.py"},
        {"filename": "backend/app/two.py"},
    ]

    forward = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue_one, issue_two],
        authoritative_files=authoritative_files,
    )
    assert {c["file_path"] for c in forward} == {"backend/app/one.py", "backend/app/two.py"}

    reversed_order = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue_two, issue_one],
        authoritative_files=authoritative_files,
    )
    assert {c["file_path"] for c in reversed_order} == {
        "backend/app/one.py",
        "backend/app/two.py",
    }


@pytest.mark.asyncio
async def test_unverified_prs_do_not_cross_verified_escalation_threshold():
    project_id = uuid.uuid4()
    session = _Session()
    for pr_number in (201, 202, 203):
        context = _context("same concern remains", f"issue-{pr_number}")
        context.pop("_pr_changed_files_github_verified")
        task = Task(project_id=project_id, context=context)
        await record_merged_pr_finding_chains(task, session, pr_number=pr_number)

    assert session.rows[0].occurrences == 3
    assert session.rows[0].verified_occurrences == 0
    assert (
        await find_and_mark_chain_escalation(
            session,
            project_id=project_id,
            issues=[
                {
                    "severity": "major",
                    "description": "same concern remains",
                    "file": "backend/app/example.py",
                }
            ],
            authoritative_files=None,
        )
        == []
    )


@pytest.mark.asyncio
async def test_pr_history_is_capped_but_occurrences_remain_authoritative():
    project_id = uuid.uuid4()
    session = _Session()
    for pr_number in range(301, 326):
        task = Task(project_id=project_id, context=_context("same concern", f"issue-{pr_number}"))
        await record_merged_pr_finding_chains(task, session, pr_number=pr_number)

    chain = session.rows[0]
    assert chain.occurrences == 25
    assert len(chain.pr_numbers) == 20
    assert chain.pr_numbers == list(range(306, 326))


def _passing_gate_receipt(*, suppressions: dict | None = None) -> dict:
    receipt = {
        "status": "passed",
        "command": "./check test",
        "timeout_seconds": 600,
        "hard_timeout_seconds": 1800,
        "elapsed_seconds": 5,
        "deadline_extended": False,
        "exit_code": 0,
        "authoritative_ci_required": False,
        "recorded_at": _utcnow().isoformat(),
    }
    if suppressions is not None:
        receipt["guard_suppressions"] = suppressions
        receipt["guard_suppressions_counts"] = {
            "observed_lanes": len(suppressions),
            "kept_lanes": len(suppressions),
        }
    return receipt


def test_guard_suppression_extractor_fails_closed_for_truncation_and_staleness():
    identity = "backend/testing/test_x.py::test_x::AssertionError"
    cross_lane_truncated = _passing_gate_receipt(
        suppressions={"backend-pytest": {"state": "passed", "suppressed": {}}}
    )
    cross_lane_truncated["guard_suppressions_counts"] = {
        "observed_lanes": 13,
        "kept_lanes": 1,
    }
    assert extract_guard_suppression_identities(cross_lane_truncated) is None

    legacy_truncated = _passing_gate_receipt()
    legacy_truncated["suppressed_failures"] = {}
    legacy_truncated["attribution_counts"] = {
        "suppressed_failures": {"observed": 1, "kept": 0}
    }
    assert extract_guard_suppression_identities(legacy_truncated) is None

    stale = _passing_gate_receipt(
        suppressions={
            "backend-pytest": {
                "state": "preexisting_test_failure",
                "suppressed": {identity: "inherited"},
            }
        }
    )
    stale["recorded_at"] = (_utcnow() - timedelta(days=2)).isoformat()
    assert extract_guard_suppression_identities(stale) is None


def test_guard_suppression_extractor_uses_cross_lane_projection_over_legacy():
    identity = "backend/testing/test_x.py::test_x::AssertionError"
    receipt = _passing_gate_receipt(
        suppressions={
            "backend-pytest": {
                "state": "preexisting_test_failure",
                "suppressed": {identity: "inherited"},
            }
        }
    )
    receipt["suppressed_failures"] = {f"[backend-pytest] {identity}": "inherited"}
    receipt["attribution_counts"] = {
        "suppressed_failures": {"observed": 1, "kept": 1}
    }

    assert extract_guard_suppression_identities(receipt) == [("backend-pytest", identity)]


class _ConflictOnceSession(_Session):
    """Model a first-insert race won by another independent task."""

    def __init__(self, source_task_id: str):
        super().__init__()
        self._source_task_id = source_task_id
        self._conflicted = False

    async def flush(self, _objects=None):
        if self._conflicted:
            return None
        self._conflicted = True
        attempted = self.rows[0]
        self.rows[:] = [
            ModuleFindingChain(
                project_id=attempted.project_id,
                finding_key=attempted.finding_key,
                file_path=attempted.file_path,
                summary=attempted.summary,
                chain_kind=attempted.chain_kind,
                suppression_lane=attempted.suppression_lane,
                failure_identity=attempted.failure_identity,
                source_task_ids=[self._source_task_id],
                pr_numbers=[],
                first_seen_pr=0,
                last_seen_pr=0,
                occurrences=1,
                verified_occurrences=1,
                status="open",
            )
        ]
        raise IntegrityError("insert", {}, Exception("unique conflict"))


@pytest.mark.asyncio
async def test_guard_suppression_retries_current_receipt_after_insert_race():
    project_id = uuid.uuid4()
    identity = "backend/testing/test_x.py::test_x::AssertionError"
    task = Task(
        project_id=project_id,
        context={
            "publication_gate_receipt": _passing_gate_receipt(
                suppressions={
                    "backend-pytest": {
                        "state": "preexisting_test_failure",
                        "suppressed": {identity: "inherited"},
                    }
                }
            )
        },
    )
    session = _ConflictOnceSession(str(uuid.uuid4()))

    touched = await ingest_guard_suppression_receipt(task, session)

    assert len(touched) == 1
    assert session.rows[0].occurrences == 2
    assert str(task.id) in session.rows[0].source_task_ids
    assert _guard_chain_key("backend-pytest", identity) in task.context[
        "_guard_suppression_recorded_keys"
    ]


@pytest.mark.asyncio
async def test_blocked_receipt_context_omits_the_removed_incomplete_flag():
    task = Task(
        project_id=uuid.uuid4(),
        context={"publication_gate_receipt": {"status": "passed"}},
    )

    assert await ingest_guard_suppression_receipt(task, _Session()) == []
    assert "guard_suppression_receipt_incomplete" not in task.context


@pytest.mark.asyncio
async def test_recurring_guard_suppression_has_one_owner_and_resolves():
    """Three tasks with one structured baseline failure make one owned fix.

    This deliberately replays the first task's heartbeat before the threshold:
    receipt history and heartbeats are diagnostic evidence, not extra chain
    occurrences. The remediation can resolve only after its own clean receipt
    lands, never because an unrelated task happened to merge.
    """
    project_id = uuid.uuid4()
    session = _Session()
    suppression = {
        "backend-pytest": {
            "state": "preexisting_test_failure",
            "suppressed": {
                "backend/testing/test_x.py::test_x::AssertionError": "inherited"
            },
        }
    }
    source_tasks = [
        Task(project_id=project_id, context={"publication_gate_receipt": _passing_gate_receipt(suppressions=suppression)})
        for _ in range(CHAIN_ESCALATION_THRESHOLD)
    ]

    await ingest_guard_suppression_receipt(source_tasks[0], session)
    source_tasks[0].context["publication_gate_receipt_history"] = [
        _passing_gate_receipt(suppressions=suppression)
    ]
    await ingest_guard_suppression_receipt(source_tasks[0], session)  # heartbeat replay
    for task in source_tasks[1:]:
        await ingest_guard_suppression_receipt(task, session)

    assert len(session.rows) == 1
    chain = session.rows[0]
    assert chain.chain_kind == "guard_suppression"
    assert chain.occurrences == CHAIN_ESCALATION_THRESHOLD
    assert chain.source_task_ids == [str(task.id) for task in source_tasks]
    # Ingestion records evidence and nothing else. It runs inside the agent
    # heartbeat, which already holds its own Task row FOR UPDATE and therefore
    # cannot hold the Project admission a Task INSERT requires.
    assert chain.remediation_task_id is None
    assert _remediation_owners(session) == []

    assert len(await _elect(session)) == 1
    assert chain.remediation_task_id is not None
    owners = [task for task in session.added_tasks if task.id == chain.remediation_task_id]
    assert len(owners) == 1
    owner = owners[0]
    # An owned chain is no longer a candidate, so the sweep is idempotent.
    assert await _elect(session) == []
    assert _remediation_owners(session) == [owner]
    assert owner.context["guard_suppression_remediation_keys"] == [chain.finding_key]

    # An unrelated clean landing has no authority to resolve the chain.
    unrelated = Task(project_id=project_id, context={"publication_gate_receipt": _passing_gate_receipt()})
    assert await resolve_landed_guard_suppression_remediation(unrelated, session) == []
    assert chain.status == "open"

    owner.context["publication_gate_receipt"] = _passing_gate_receipt()
    resolved = await resolve_landed_guard_suppression_remediation(owner, session)
    assert resolved == [chain]
    assert chain.status == "resolved"
    assert chain.resolved_by_task_id == owner.id

    # Recurrence starts a new threshold window and cannot reuse the resolved
    # owner. It reaches one new owner only after three new source tasks.
    recurring_tasks = [
        Task(project_id=project_id, context={"publication_gate_receipt": _passing_gate_receipt(suppressions=suppression)})
        for _ in range(CHAIN_ESCALATION_THRESHOLD)
    ]
    for task in recurring_tasks:
        await ingest_guard_suppression_receipt(task, session)
    assert chain.status == "open"
    assert chain.occurrences == CHAIN_ESCALATION_THRESHOLD
    assert chain.remediation_task_id is None
    assert len(await _elect(session)) == 1
    assert chain.remediation_task_id != owner.id
    assert len([task for task in session.added_tasks if task.id == chain.remediation_task_id]) == 1


@pytest.mark.asyncio
async def test_landed_remediation_resolves_aggregate_escalation_message():
    project_id = uuid.uuid4()
    owner = Task(project_id=project_id, context={"publication_gate_receipt": _passing_gate_receipt()})
    chain = ModuleFindingChain(
        project_id=project_id,
        finding_key="finding",
        file_path="guard:backend-pytest",
        summary="Recurring baseline guard suppression",
        chain_kind="guard_suppression",
        source_task_ids=[],
        pr_numbers=[],
        first_seen_pr=0,
        last_seen_pr=0,
        remediation_task_id=owner.id,
    )
    message = ProjectMessage(
        project_id=project_id,
        idempotency_key=f"guard-suppression:{project_id}:{chain.finding_key}:escalation",
    )
    session = _Session()
    session.rows.append(chain)
    session.messages.append(message)

    async def _scalar(_statement):
        return None

    session.scalar = _scalar
    assert await resolve_landed_guard_suppression_remediation(
        Task(
            id=owner.id,
            project_id=project_id,
            context={
                "publication_gate_receipt": _passing_gate_receipt(),
                "guard_suppression_remediation_keys": [chain.finding_key],
            },
        ),
        session,
    ) == [chain]
    assert message.status == "resolved"
    assert message.resolution == "guard_suppression_remediated"
    assert message.resolved_at is not None


def test_ordinary_two_or_three_cycle_intra_pr_convergence_does_not_escalate():
    task_rows = [
        Task(
            payloads=[{"type": "review"}],
            context={
                "review_loop": {
                    "original_task_id": "same-pr",
                    "iteration": iteration,
                }
            },
        )
        for iteration in (1, 2, 3)
    ]
    assert build_cross_episode_abandon_candidate(task_rows) is None


def _remediation_owners(session) -> list[Task]:
    """Only the elected owners; ingestion also re-adds its own source task."""
    return [
        task
        for task in session.added_tasks
        if task.created_by == "SYSTEM_AUTO_REMEDIATION"
    ]


async def _elect(session, decision: str = AUTOMATIC_TASK_CREATION_ALLOWED) -> list[Task]:
    """Drive the leader-owned election sweep against a ledger fake session.

    The Project admission itself is stubbed here because these fakes have no
    project table; ``test_owner_election_honours_real_project_authority`` and
    ``test_owner_election_refuses_a_paused_project`` exercise the real
    ``acquire_automatic_task_creation_authority`` against the database.
    """
    with patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new=AsyncMock(return_value=decision),
    ):
        return await elect_guard_suppression_owners(session)


class _TaskStoreSession(_Session):
    """A ledger session that can also resolve a remediation owner by id."""

    def __init__(self):
        super().__init__()
        self.tasks: dict[uuid.UUID, Task] = {}

    def add(self, value):
        super().add(value)
        if isinstance(value, Task):
            self.tasks[value.id] = value

    async def get(self, _model, key):
        return self.tasks.get(key)


def _guard_suppression(identity: str) -> dict:
    return {
        "backend-pytest": {
            "state": "preexisting_test_failure",
            "suppressed": {identity: "inherited"},
        }
    }


@pytest.mark.asyncio
async def test_stale_owner_receipt_still_resolves_its_landed_chain():
    """A remediation PR that sat in review past the freshness window still resolves.

    The receipt is written by the owner's own ``./check`` run and read when that
    same owner's PR merges, so it is already scoped to one task lifetime. Before
    this, a >24h review/CI cycle stranded the chain permanently open: it could
    never resolve (stale receipt) and never re-escalate (owner already set).
    """
    project_id = uuid.uuid4()
    chain = ModuleFindingChain(
        project_id=project_id,
        finding_key="finding",
        file_path="guard:backend-pytest",
        summary="Recurring baseline guard suppression",
        chain_kind="guard_suppression",
        source_task_ids=[],
        pr_numbers=[],
        first_seen_pr=0,
        last_seen_pr=0,
        status="open",
    )
    session = _Session()
    session.rows.append(chain)

    for offset in (timedelta(days=9), -timedelta(minutes=5)):
        # Both a long review cycle and agent-VM clock skew into the future.
        receipt = _passing_gate_receipt()
        receipt["recorded_at"] = (_utcnow() - offset).isoformat()
        # Ingestion still refuses the same receipt: only resolution is exempt.
        assert extract_guard_suppression_identities(receipt) is None
        assert extract_guard_suppression_identities(receipt, require_fresh=False) == []

        chain.status = "open"
        chain.resolved_by_task_id = None
        chain.resolved_at = None
        owner = Task(
            project_id=project_id,
            context={
                "publication_gate_receipt": receipt,
                "guard_suppression_remediation_keys": [chain.finding_key],
            },
        )
        chain.remediation_task_id = owner.id

        assert await resolve_landed_guard_suppression_remediation(owner, session) == [chain]
        assert chain.status == "resolved"
        assert chain.resolved_by_task_id == owner.id


@pytest.mark.asyncio
async def test_resolution_still_refuses_dirty_or_malformed_owner_receipts():
    """Freshness exemption must not weaken any other completeness rule."""
    project_id = uuid.uuid4()
    chain = ModuleFindingChain(
        project_id=project_id,
        finding_key="finding",
        file_path="guard:backend-pytest",
        summary="Recurring baseline guard suppression",
        chain_kind="guard_suppression",
        source_task_ids=[],
        pr_numbers=[],
        first_seen_pr=0,
        last_seen_pr=0,
        status="open",
    )
    session = _Session()
    session.rows.append(chain)

    dirty = _passing_gate_receipt(
        suppressions=_guard_suppression("backend/testing/test_x.py::test_x::AssertionError")
    )
    truncated = _passing_gate_receipt(suppressions={"backend-pytest": {"state": "passed", "suppressed": {}}})
    truncated["guard_suppressions_counts"] = {"observed_lanes": 13, "kept_lanes": 1}
    failing = _passing_gate_receipt()
    failing["status"] = "failed"
    unstamped = _passing_gate_receipt()
    unstamped.pop("recorded_at")
    unparseable = _passing_gate_receipt()
    unparseable["recorded_at"] = "not-a-timestamp"

    for receipt in (dirty, truncated, failing, unstamped, unparseable):
        assert extract_guard_suppression_identities(receipt, require_fresh=False) != []
        owner = Task(
            project_id=project_id,
            context={
                "publication_gate_receipt": receipt,
                "guard_suppression_remediation_keys": [chain.finding_key],
            },
        )
        chain.remediation_task_id = owner.id
        assert await resolve_landed_guard_suppression_remediation(owner, session) == []
        assert chain.status == "open"


@pytest.mark.asyncio
async def test_recurrence_re_elects_owner_after_the_first_one_failed():
    """A dead owner must not hold a chain open forever."""
    project_id = uuid.uuid4()
    identity = "backend/testing/test_x.py::test_x::AssertionError"
    session = _TaskStoreSession()

    def _source() -> Task:
        return Task(
            project_id=project_id,
            context={
                "publication_gate_receipt": _passing_gate_receipt(
                    suppressions=_guard_suppression(identity)
                )
            },
        )

    def _owners() -> list[Task]:
        return [
            task
            for task in session.added_tasks
            if task.created_by == "SYSTEM_AUTO_REMEDIATION"
        ]

    for _ in range(CHAIN_ESCALATION_THRESHOLD):
        await ingest_guard_suppression_receipt(_source(), session)
    chain = session.rows[0]
    assert await _elect(session)
    first_owner_id = chain.remediation_task_id
    assert first_owner_id is not None

    # The owner is still running: a new recurrence must NOT elect a second one.
    await ingest_guard_suppression_receipt(_source(), session)
    assert await _elect(session) == []
    assert chain.remediation_task_id == first_owner_id
    assert len(_owners()) == 1

    # A COMPLETED owner is left alone too — its landed-signal resolution may
    # still be in flight, and a second owner would make that resolution unable
    # to match remediation_task_id.
    session.tasks[first_owner_id].status = TaskStatus.COMPLETED
    await ingest_guard_suppression_receipt(_source(), session)
    assert await _elect(session) == []
    assert chain.remediation_task_id == first_owner_id
    assert len(_owners()) == 1

    # A FAILED owner is never coming back: ingestion releases it and the next
    # sweep elects a new one under the project's authority.
    session.tasks[first_owner_id].status = TaskStatus.FAILED
    await ingest_guard_suppression_receipt(_source(), session)
    assert chain.remediation_task_id is None
    assert await _elect(session)
    assert chain.status == "open"
    assert chain.remediation_task_id is not None
    assert chain.remediation_task_id != first_owner_id
    assert len(_owners()) == 2


def _mature_chain(project_id) -> ModuleFindingChain:
    """A persisted chain that is open, at threshold, and unowned."""
    return ModuleFindingChain(
        project_id=project_id,
        finding_key=_guard_chain_key("backend-pytest", "backend/testing/test_x.py::test_x::AssertionError"),
        file_path="guard:backend-pytest",
        summary="Recurring baseline guard suppression",
        chain_kind="guard_suppression",
        suppression_lane="backend-pytest",
        failure_identity="backend/testing/test_x.py::test_x::AssertionError",
        source_task_ids=[],
        pr_numbers=[],
        first_seen_pr=0,
        last_seen_pr=0,
        occurrences=CHAIN_ESCALATION_THRESHOLD,
        verified_occurrences=CHAIN_ESCALATION_THRESHOLD,
        status="open",
    )


@pytest.mark.asyncio
async def test_election_denied_by_project_authority_elects_nothing():
    """A refused admission leaves the chain intact for a later sweep."""
    session = _Session()
    chain = _mature_chain(uuid.uuid4())
    session.rows.append(chain)

    assert await _elect(session, AUTOMATIC_TASK_CREATION_PROJECT_PAUSED) == []
    assert chain.remediation_task_id is None
    assert _remediation_owners(session) == []

    assert len(await _elect(session)) == 1
    assert chain.remediation_task_id is not None


@pytest.mark.asyncio
async def test_owner_election_honours_real_project_authority(db_session):
    """The sweep takes the real Project admission before inserting the owner."""
    project = Project(name="guard election", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    chain = _mature_chain(project.id)
    db_session.add(chain)
    await db_session.commit()

    elected = await elect_guard_suppression_owners(db_session)
    await db_session.commit()

    assert len(elected) == 1
    owners = list(
        (
            await db_session.execute(select(Task).where(Task.project_id == project.id))
        )
        .scalars()
        .all()
    )
    assert [task.created_by for task in owners] == ["SYSTEM_AUTO_REMEDIATION"]
    assert owners[0].status == TaskStatus.QUEUED
    assert owners[0].context["guard_suppression_remediation_keys"] == [
        chain.finding_key
    ]
    await db_session.refresh(chain)
    assert chain.remediation_task_id == owners[0].id

    # Owned chains are no longer candidates, so re-running the sweep is a no-op.
    assert await elect_guard_suppression_owners(db_session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["paused", "archived"])
async def test_owner_election_refuses_a_project_that_cannot_take_new_work(
    db_session, blocked
):
    """A paused or archived project must not accumulate latent queued work.

    This is exactly the invariant the earlier heartbeat-side election could not
    hold: it ran behind the reporting task's row lock and so never consulted the
    project at all. Electing here, under the Project lock, is what makes the
    remediation owner an ordinary admitted automatic task.
    """
    project = Project(
        name=f"guard election {blocked}",
        user_id="test-user-123",
        **{blocked: True},
    )
    db_session.add(project)
    await db_session.flush()
    chain = _mature_chain(project.id)
    db_session.add(chain)
    await db_session.commit()

    assert await elect_guard_suppression_owners(db_session) == []
    await db_session.commit()

    await db_session.refresh(chain)
    assert chain.remediation_task_id is None
    assert (
        list(
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        == []
    )
