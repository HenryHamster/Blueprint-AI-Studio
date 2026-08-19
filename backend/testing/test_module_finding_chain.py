from __future__ import annotations

import uuid
from contextlib import nullcontext

import pytest

from app.models import ModuleFindingChain, Task
from app.module_finding_chain import (
    CHAIN_ESCALATION_THRESHOLD,
    find_and_mark_chain_escalation,
    record_merged_pr_finding_chains,
    resolved_review_findings,
)
from app.review_loop_convergence import build_cross_episode_abandon_candidate


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

    async def execute(self, _statement):
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
    candidate = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue],
        authoritative_files=[{"filename": "backend/app/example.py"}],
    )
    assert candidate is not None
    assert candidate["status"] == "chain_escalation"
    assert candidate["occurrences"] == CHAIN_ESCALATION_THRESHOLD
    assert chain.status == "escalated"

    # Re-entry on the same PR without a new merged occurrence: the chain is
    # still past threshold, so the detector finds it again. It is not a
    # lifetime suppressor — dedup for repeated re-entry on one PR happens
    # downstream, in the alert layer (alert_dispatch, keyed by
    # (task_id, title) where the title encodes the PR number).
    same_pr_candidate = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue],
        authoritative_files=[{"filename": "backend/app/example.py"}],
    )
    assert same_pr_candidate is not None
    assert same_pr_candidate["finding_key"] == candidate["finding_key"]

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

    next_pr_candidate = await find_and_mark_chain_escalation(
        session,
        project_id=project_id,
        issues=[issue],
        authoritative_files=[{"filename": "backend/app/example.py"}],
    )
    assert next_pr_candidate is not None
    assert next_pr_candidate["finding_key"] == candidate["finding_key"]
    assert next_pr_candidate["occurrences"] == 4
    assert chain.status == "escalated"


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
        is None
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
