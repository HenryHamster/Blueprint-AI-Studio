"""Criterion named-test trial lifecycle tests split from test_acceptance_criteria.py.

Covers trial enrollment debounce (one in-flight per criterion), terminal-identity
re-arm suppression, the consecutive-non-green streak suppressor and its re-arm
legs, work-freeze/autonomy admission checks, and stale-queued-trial supersede.
Split as a companion so the parent module stays below its governed size cap; the
shared project/version factories remain defined in the parent so database setup
behavior has one source of truth.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app import acceptance_criteria as ac
from app.models import Task, TaskStatus
from app.time_utils import _utcnow
from testing.test_acceptance_criteria import (
    _make_project,
    _make_version,
    _select_criterion_trials,
)

# ---------------------------------------------------------------------------
# Consecutive-non-green streak suppression: the trial_identity seed includes
# head_sha, so a project whose candidate SHA bumps continuously can re-arm
# the SAME (criterion, test, precondition) triple forever even though the
# execution precondition never changed. This is leg 1 of the fix for the
# 99-placements/6-criteria/week defect: 33x the healthy re-arm ceiling.
# ---------------------------------------------------------------------------


def _mark_trial_non_green(task, *, could_not_run: bool = True) -> None:
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        "criterion_named_test_trial_contradiction": {
            "could_not_run": could_not_run,
            "ran": not could_not_run,
            "passed": False,
        },
    }


@pytest.mark.asyncio
async def test_criterion_trial_suppresses_after_consecutive_non_green_streak_across_shas(
    db_session,
):
    """A bare head_sha bump must not re-arm a repeatedly-non-green trial.

    Before the fix, ``trial_identity`` seeds on ``head_sha`` so every SHA bump
    mints a brand-new identity and the enroller happily re-enrolls forever —
    this is the O(criteria x SHA bumps) spam mechanism the task describes.
    After the fix, once
    ``CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD``
    consecutive terminal trials for the SAME test+precondition identity come
    back non-green (independent of head_sha), enrollment stops.
    """
    project = await _make_project(db_session, "CriterionTrialStreakSuppression")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Streak-suppressed criterion"
    )
    threshold = ac.CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD
    assert threshold >= 2, "test assumes more than one trial is required to suppress"

    trial = None
    for index in range(threshold - 1):
        await _set_candidate_sha(db_session, version, f"{index}" * 40)
        trial = await _enroll_trial(
            db_session, project, version, criterion, "testing/test_streak.py"
        )
        assert trial is not None, (
            f"trial #{index} must still enroll — only {index} prior non-green "
            f"streak entries, below the {threshold} suppression bound"
        )
        _mark_trial_non_green(trial)
        db_session.add(trial)
        await db_session.commit()

    # One more non-green trial reaches the threshold.
    await _set_candidate_sha(db_session, version, "f" * 40)
    trial = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak.py"
    )
    assert trial is not None
    _mark_trial_non_green(trial)
    db_session.add(trial)
    await db_session.commit()

    # A brand-new candidate SHA alone must NOT re-arm the streak.
    await _set_candidate_sha(db_session, version, "9" * 40)
    suppressed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak.py"
    )
    assert suppressed is None, (
        "a bare head_sha bump re-armed a trial whose test+precondition identity "
        "has produced only non-green terminal outcomes — the suppression bound "
        "is not doing its job"
    )

    trials = await _select_criterion_trials(db_session, project.id)
    matching = [
        t for t in trials
        if t.first_payload.get("criterion_key") == criterion.criterion_key
    ]
    assert len(matching) == threshold, (
        "no additional trial task may be minted once the streak bound is reached"
    )


@pytest.mark.asyncio
async def test_criterion_trial_streak_suppression_rearms_on_changed_test_identity(
    db_session,
):
    """A changed declared covering-test set must re-arm immediately, even
    while the streak-suppression bound would otherwise hold enrollment shut."""
    project = await _make_project(db_session, "CriterionTrialStreakTestIdentity")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Streak test-identity criterion"
    )
    threshold = ac.CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD

    for index in range(threshold):
        await _set_candidate_sha(db_session, version, f"{index}" * 40)
        trial = await _enroll_trial(
            db_session, project, version, criterion, "testing/test_streak_old.py"
        )
        assert trial is not None
        _mark_trial_non_green(trial)
        db_session.add(trial)
        await db_session.commit()

    await _set_candidate_sha(db_session, version, "9" * 40)
    still_suppressed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak_old.py"
    )
    assert still_suppressed is None, "sanity check: streak must be suppressing here"

    # The declared covering tests changed — test_identity changes, and the
    # criterion must re-arm even at the SAME suppressed head_sha.
    rearmed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak_new.py"
    )
    assert rearmed is not None, (
        "a changed test_identity must re-arm enrollment even under an active "
        "consecutive-non-green streak on the OLD test identity"
    )


@pytest.mark.asyncio
async def test_criterion_trial_streak_suppression_rearms_on_changed_precondition_identity(
    db_session,
):
    """A changed execution-precondition contract must re-arm immediately,
    even while the streak-suppression bound would otherwise hold shut."""
    project = await _make_project(db_session, "CriterionTrialStreakPrecondition")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Streak precondition criterion"
    )
    threshold = ac.CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD

    for index in range(threshold):
        await _set_candidate_sha(db_session, version, f"{index}" * 40)
        trial = await _enroll_trial(
            db_session, project, version, criterion, "testing/test_streak_precond.py"
        )
        assert trial is not None
        _mark_trial_non_green(trial)
        db_session.add(trial)
        await db_session.commit()

    await _set_candidate_sha(db_session, version, "9" * 40)
    still_suppressed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak_precond.py"
    )
    assert still_suppressed is None, "sanity check: streak must be suppressing here"

    with patch.object(
        ac,
        "CRITERION_NAMED_TEST_TRIAL_CONTRACT",
        f"{ac.CRITERION_NAMED_TEST_TRIAL_CONTRACT}:repaired-runner-v2",
    ):
        rearmed = await _enroll_trial(
            db_session, project, version, criterion, "testing/test_streak_precond.py"
        )
    assert rearmed is not None, (
        "a repaired execution precondition must re-arm enrollment even under "
        "an active consecutive-non-green streak on the OLD precondition identity"
    )


@pytest.mark.asyncio
async def test_criterion_trial_streak_suppression_rearms_after_recency_window_elapses(
    db_session,
):
    """A suppressed streak of pure infrastructure misses (``could_not_run``)
    must self-heal once the recency window elapses, with NO change to
    test_identity or precondition_identity — an environment repair (e.g. a
    verification-toolchain venv fix) cannot otherwise ever re-arm the
    criterion, since neither identity is sensitive to that repair."""
    project = await _make_project(db_session, "CriterionTrialStreakRecencyRearm")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Streak recency-rearm criterion"
    )
    threshold = ac.CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD

    for index in range(threshold):
        await _set_candidate_sha(db_session, version, f"{index}" * 40)
        trial = await _enroll_trial(
            db_session, project, version, criterion, "testing/test_streak_recency.py"
        )
        assert trial is not None
        _mark_trial_non_green(trial, could_not_run=True)
        db_session.add(trial)
        await db_session.commit()

    await _set_candidate_sha(db_session, version, "9" * 40)
    still_suppressed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak_recency.py"
    )
    assert still_suppressed is None, "sanity check: streak must be suppressing here"

    # Push every existing trial for this identity outside the recency window —
    # simulating that the underlying infra was repaired days ago and the
    # criterion has simply not been re-attempted since.
    stale_cutoff = _utcnow() - timedelta(
        days=ac.CRITERION_NAMED_TEST_TRIAL_STREAK_RECENCY_WINDOW_DAYS + 1
    )
    trials = await _select_criterion_trials(db_session, project.id)
    for task in trials:
        if task.first_payload.get("criterion_key") == criterion.criterion_key:
            task.created_at = stale_cutoff
            db_session.add(task)
    await db_session.commit()

    rearmed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak_recency.py"
    )
    assert rearmed is not None, (
        "a streak of purely stale (outside the recency window) non-green "
        "trials must re-arm enrollment even though test_identity and "
        "precondition_identity are unchanged"
    )


@pytest.mark.asyncio
async def test_criterion_trial_streak_suppression_ignores_cancelled_superseded_rows(
    db_session,
):
    """A CANCELLED (superseded-before-running) trial carries no pass/fail
    signal and must neither count toward nor break the non-green streak."""
    project = await _make_project(db_session, "CriterionTrialStreakCancelledNoise")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Streak cancelled-noise criterion"
    )
    threshold = ac.CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD

    for index in range(threshold):
        await _set_candidate_sha(db_session, version, f"{index}" * 40)
        trial = await _enroll_trial(
            db_session, project, version, criterion, "testing/test_streak_cancel.py"
        )
        assert trial is not None
        _mark_trial_non_green(trial)
        db_session.add(trial)
        await db_session.commit()

        # Interleave a CANCELLED noise row for the same identity — it must be
        # skipped by the streak scan, not treated as a green break.
        noise = Task(
            project_id=project.id,
            title="Cancelled noise trial",
            payloads=[trial.first_payload],
            context=dict(trial.context or {}),
            status=TaskStatus.CANCELLED,
            created_by="SYSTEM_AUTO_REVIEW",
        )
        noise.context.pop("criterion_named_test_trial_contradiction", None)
        noise.context["superseded_by_newer_candidate"] = True
        db_session.add(noise)
        await db_session.commit()

    await _set_candidate_sha(db_session, version, "9" * 40)
    suppressed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak_cancel.py"
    )
    assert suppressed is None, (
        "interleaved CANCELLED rows must not prevent the non-green streak from "
        "being detected"
    )


@pytest.mark.asyncio
async def test_criterion_trial_streak_suppression_survives_heavy_cancelled_supersede_churn(
    db_session,
):
    """The old fixed scan pad (threshold + 5 = 8 rows) let CANCELLED
    supersede noise push real terminal signal out of the streak window under
    VM-saturation churn, where a QUEUED trial is superseded before it can
    run on nearly every candidate SHA bump. With the scan restricted to
    signal-bearing rows (a contradiction stamp or a COMPLETED row), the
    streak must still be detected even with a heavy volume of interleaved
    CANCELLED rows — far past the old pad."""
    project = await _make_project(
        db_session, "CriterionTrialStreakHeavyCancelledChurn"
    )
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Streak heavy-churn criterion"
    )
    threshold = ac.CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD
    cancelled_per_terminal = 20

    for index in range(threshold):
        await _set_candidate_sha(db_session, version, f"{index}" * 40)
        trial = await _enroll_trial(
            db_session, project, version, criterion, "testing/test_streak_churn.py"
        )
        assert trial is not None
        _mark_trial_non_green(trial)
        db_session.add(trial)
        await db_session.commit()

        # Simulate heavy supersede churn: many more CANCELLED noise rows per
        # terminal trial than the old fixed scan pad (threshold + 5 = 8) could
        # tolerate before pushing real signal out of the window.
        for noise_index in range(cancelled_per_terminal):
            noise = Task(
                project_id=project.id,
                title=f"Cancelled churn noise trial {index}-{noise_index}",
                payloads=[trial.first_payload],
                context=dict(trial.context or {}),
                status=TaskStatus.CANCELLED,
                created_by="SYSTEM_AUTO_REVIEW",
            )
            noise.context.pop("criterion_named_test_trial_contradiction", None)
            noise.context["superseded_by_newer_candidate"] = True
            db_session.add(noise)
        await db_session.commit()

    await _set_candidate_sha(db_session, version, "9" * 40)
    suppressed = await _enroll_trial(
        db_session, project, version, criterion, "testing/test_streak_churn.py"
    )
    assert suppressed is None, (
        "heavy CANCELLED supersede churn (well past the old fixed scan pad) "
        "must not defeat the non-green streak suppression"
    )


# ---------------------------------------------------------------------------
# In-flight debounce: at most one non-terminal criterion_named_test_trial per
# criterion + release version (root-fix for the O(criteria x SHA bumps) spam
# mechanism — see enroll_criterion_named_test_trial).
# ---------------------------------------------------------------------------

_TASK_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def _set_candidate_sha(db_session, version, sha: str) -> None:
    version.last_qualification = {"status": "scoring", "candidate_sha": sha}
    db_session.add(version)
    await db_session.commit()


async def _enroll_trial(db_session, project, version, criterion, ref):
    return await ac.enroll_criterion_named_test_trial(
        db_session, project, version=version,
        criterion_key=criterion.criterion_key, criterion_text=criterion.text,
        test_refs=[ref],
    )


@pytest.mark.asyncio
async def test_criterion_trial_inflight_queued_same_head_skips_new_enrollment(db_session):
    """A QUEUED trial pinned to the CURRENT head suppresses a duplicate enroll."""
    project = await _make_project(db_session, "CriterionTrialInflightSameHead")
    version = await _make_version(db_session, project)
    await _set_candidate_sha(db_session, version, "1" * 40)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "same-head")
    ref = "testing/test_inflight_same_head.py"

    first = await _enroll_trial(db_session, project, version, criterion, ref)
    assert first is not None and first.status == TaskStatus.QUEUED

    second = await _enroll_trial(db_session, project, version, criterion, ref)
    assert second is None, "an in-flight QUEUED trial at the same head must debounce"

    trials = await _select_criterion_trials(db_session, project.id)
    assert [t.id for t in trials] == [first.id]
    await db_session.refresh(first)
    assert first.status == TaskStatus.QUEUED, "the original trial must be untouched"


@pytest.mark.asyncio
async def test_criterion_trial_inflight_queued_stale_head_is_superseded(db_session):
    """A QUEUED trial on a stale head is cancelled+superseded; a fresh trial enrolls.

    Exactly one non-terminal trial must remain for this criterion afterward.
    """
    project = await _make_project(db_session, "CriterionTrialInflightStaleHead")
    version = await _make_version(db_session, project)
    await _set_candidate_sha(db_session, version, "2" * 40)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "stale-head")
    ref = "testing/test_inflight_stale_head.py"

    stale = await _enroll_trial(db_session, project, version, criterion, ref)
    assert stale is not None and stale.status == TaskStatus.QUEUED

    await _set_candidate_sha(db_session, version, "3" * 40)  # candidate SHA moves
    fresh = await _enroll_trial(db_session, project, version, criterion, ref)
    assert fresh is not None, "a stale QUEUED trial must be superseded, not blocking"
    assert fresh.first_payload["candidate_identity"] == "3" * 40

    await db_session.refresh(stale)
    assert stale.status == TaskStatus.CANCELLED
    assert stale.context.get("superseded_by_newer_candidate") is True
    assert stale.context.get("superseded_stale_head_sha") == "2" * 40
    assert stale.context.get("superseded_successor_head_sha") == "3" * 40
    assert "superseded" in (stale.latest_status_message or "").lower()

    trials = await _select_criterion_trials(db_session, project.id)
    assert {t.id for t in trials} == {stale.id, fresh.id}
    non_terminal = [t for t in trials if t.status not in _TASK_TERMINAL_STATUSES]
    assert [t.id for t in non_terminal] == [fresh.id]


@pytest.mark.asyncio
async def test_criterion_trial_inflight_running_stale_head_blocks_new_enrollment(db_session):
    """A running (IN_PROGRESS) trial on a stale head is left alone; no new enroll.

    Yanking running work is the review-supersede-churn antipattern; the periodic
    backfill sweep enrolls the follow-up once the running trial goes terminal.
    """
    project = await _make_project(db_session, "CriterionTrialInflightRunningStale")
    version = await _make_version(db_session, project)
    await _set_candidate_sha(db_session, version, "4" * 40)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "running-stale")
    ref = "testing/test_inflight_running_stale.py"

    running = await _enroll_trial(db_session, project, version, criterion, ref)
    assert running is not None
    running.status = TaskStatus.IN_PROGRESS
    db_session.add(running)
    await db_session.commit()

    await _set_candidate_sha(db_session, version, "5" * 40)  # moves while running
    result = await _enroll_trial(db_session, project, version, criterion, ref)
    assert result is None, "a running trial on a stale head must block new enrollment"

    await db_session.refresh(running)
    assert running.status == TaskStatus.IN_PROGRESS, "running trial must not be cancelled"
    assert not running.context.get("superseded_by_newer_candidate")

    trials = await _select_criterion_trials(db_session, project.id)
    assert [t.id for t in trials] == [running.id], "no new trial must be enrolled"


@pytest.mark.asyncio
async def test_criterion_trial_terminal_identical_identity_still_suppressed(db_session):
    """Regression pin: a terminal trial with an IDENTICAL identity stays suppressed."""
    project = await _make_project(db_session, "CriterionTrialTerminalIdenticalPin")
    version = await _make_version(db_session, project)
    await _set_candidate_sha(db_session, version, "6" * 40)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "identical")
    ref = "testing/test_terminal_identical.py"

    first = await _enroll_trial(db_session, project, version, criterion, ref)
    assert first is not None
    first.status = TaskStatus.COMPLETED
    db_session.add(first)
    await db_session.commit()

    duplicate = await _enroll_trial(db_session, project, version, criterion, ref)
    assert duplicate is None, "an identical terminal trial identity must stay suppressed"

    trials = await _select_criterion_trials(db_session, project.id)
    assert [t.id for t in trials] == [first.id]


@pytest.mark.asyncio
async def test_criterion_trial_terminal_plus_new_head_enrolls_legit_rearm(db_session):
    """A terminal trial plus a new candidate head enrolls a legitimate re-arm."""
    project = await _make_project(db_session, "CriterionTrialTerminalRearm")
    version = await _make_version(db_session, project)
    await _set_candidate_sha(db_session, version, "7" * 40)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "rearm")
    ref = "testing/test_terminal_rearm.py"

    first = await _enroll_trial(db_session, project, version, criterion, ref)
    assert first is not None
    first.status = TaskStatus.COMPLETED
    db_session.add(first)
    await db_session.commit()

    await _set_candidate_sha(db_session, version, "8" * 40)
    second = await _enroll_trial(db_session, project, version, criterion, ref)
    assert second is not None, "a new candidate head must remain a legitimate re-arm"
    assert second.first_payload["candidate_identity"] == "8" * 40

    trials = await _select_criterion_trials(db_session, project.id)
    assert {t.id for t in trials} == {first.id, second.id}
    non_terminal = [t for t in trials if t.status not in _TASK_TERMINAL_STATUSES]
    assert [t.id for t in non_terminal] == [second.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("freeze", ["paused", "autonomy_off"])
async def test_criterion_trial_respects_project_work_freeze(db_session, freeze):
    project = await _make_project(db_session, f"CriterionTrialFreeze-{freeze}")
    version = await _make_version(db_session, project)
    version.last_qualification = {"status": "scoring", "candidate_sha": "f" * 40}
    if freeze == "paused":
        project.paused = True
    else:
        project.autonomous_enabled = False
    db_session.add(project)
    db_session.add(version)
    await db_session.commit()
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Frozen project criterion"
    )

    trial = await ac.enroll_criterion_named_test_trial(
        db_session,
        project,
        version=version,
        criterion_key=criterion.criterion_key,
        criterion_text=criterion.text,
        test_refs=["testing/test_frozen.py"],
    )
    assert trial is None
    assert await _select_criterion_trials(db_session, project.id) == []


@pytest.mark.asyncio
async def test_criterion_trial_rechecks_autonomy_at_locked_admission(
    db_session, mocker
):
    """A stale enabled Project snapshot cannot bypass locked autonomy policy."""

    from app.task_locking import AUTOMATIC_TASK_CREATION_AUTONOMY_DISABLED

    project = await _make_project(db_session, "CriterionTrialLockedAutonomy")
    version = await _make_version(db_session, project)
    version.last_qualification = {"status": "scoring", "candidate_sha": "e" * 40}
    db_session.add(version)
    await db_session.commit()
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Locked autonomy criterion"
    )
    assert project.autonomous_enabled is True

    locked_admission = mocker.patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new_callable=AsyncMock,
        return_value=AUTOMATIC_TASK_CREATION_AUTONOMY_DISABLED,
    )
    trial = await ac.enroll_criterion_named_test_trial(
        db_session,
        project,
        version=version,
        criterion_key=criterion.criterion_key,
        criterion_text=criterion.text,
        test_refs=["testing/test_stale_autonomy.py"],
    )

    assert trial is None
    locked_admission.assert_awaited_once_with(
        db_session,
        project.id,
        skip_locked=True,
        require_autonomous_enabled=True,
    )
    assert await _select_criterion_trials(db_session, project.id) == []

