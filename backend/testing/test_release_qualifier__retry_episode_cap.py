# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: retry-episode cap accounting.

Covers the retry-episode counter mechanics in the release qualifier:

  * _advance_qualification_retry_episode — pure unit tests (no DB).  Verifies
    that the function increments the episode counter, stores the cap, updates
    the version model attribute, and does NOT enforce the cap (episodes can
    exceed it — the cap is advisory/informational, not a gate).

  * episode > cap reporting — verifies that when retry_episode exceeds
    retry_episode_cap, both values are persisted in last_qualification so
    operators and downstream readers can see the breach.  This is the
    "report honestly" contract.

  * _redrive_infra_failure_qualification cap gate — verifies that the actual
    re-drive gate is consecutive_infra_errors (not retry_episode), confirming
    the two mechanisms are independent.  When retry_episode > retry_episode_cap
    but consecutive_infra_errors < _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS, the
    sweep re-driver still fires.  Conversely when consecutive_infra_errors
    reaches the cap, the re-driver stops even if retry_episode is zero.

Regression target: qualification retry_episode advanced to 6 against a
declared cap of 3 (64 total attempts, error smoketest_task_failed_claude_code_failed)
— the cap breach was not being reported in last_qualification, leaving
operators with no visible signal of the cap exceedance.
"""
from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from _release_qualifier_testhelpers import (
    _make_project_and_version,
    _shared_session_factory,
)
from app import release_qualifier as rq
from app.models import Alert


# ===========================================================================
# Unit tests: _advance_qualification_retry_episode (no DB, SimpleNamespace)
# ===========================================================================


def _make_fake_version(*, episode: int = 0) -> types.SimpleNamespace:
    """Minimal version stub with the one attribute _advance… writes."""
    v = types.SimpleNamespace()
    v.qualification_retry_episode = episode
    return v


class TestAdvanceQualificationRetryEpisode:
    """Pure unit tests for _advance_qualification_retry_episode.

    No database is needed: the function only reads from ``record`` (a plain
    dict) and writes to ``version.qualification_retry_episode``.
    """

    def test_increments_from_zero(self):
        """Starting at episode=0 advances to episode=1."""
        version = _make_fake_version()
        record = {"retry_episode": 0, "status": "pending"}

        result = rq._advance_qualification_retry_episode(version, record, cap=3)

        assert result["retry_episode"] == 1
        assert result["retry_episode_cap"] == 3
        assert version.qualification_retry_episode == 1

    def test_increments_from_nonzero(self):
        """Starting at episode=2 advances to episode=3."""
        version = _make_fake_version(episode=2)
        record = {"retry_episode": 2, "status": "pending"}

        result = rq._advance_qualification_retry_episode(version, record, cap=3)

        assert result["retry_episode"] == 3
        assert result["retry_episode_cap"] == 3
        assert version.qualification_retry_episode == 3

    def test_increments_when_missing_key(self):
        """Missing retry_episode key in record is treated as 0."""
        version = _make_fake_version()
        record = {"status": "pending"}  # no retry_episode key

        result = rq._advance_qualification_retry_episode(version, record, cap=5)

        assert result["retry_episode"] == 1
        assert result["retry_episode_cap"] == 5

    def test_no_enforcement_at_cap(self):
        """When episode equals cap, the function still increments (no enforcement).

        The cap is advisory — operators read it to diagnose the breach;
        it does NOT prevent the episode counter from advancing past it.
        """
        version = _make_fake_version(episode=3)
        record = {"retry_episode": 3}

        result = rq._advance_qualification_retry_episode(version, record, cap=3)

        # Episode EXCEEDS cap — this is the behavior to document.
        assert result["retry_episode"] == 4
        assert result["retry_episode_cap"] == 3
        assert result["retry_episode"] > result["retry_episode_cap"], (
            "retry_episode must exceed retry_episode_cap when called at cap "
            "(no enforcement in _advance_qualification_retry_episode itself)"
        )
        assert version.qualification_retry_episode == 4

    def test_no_enforcement_beyond_cap(self):
        """When episode already exceeds cap, the function continues incrementing."""
        version = _make_fake_version(episode=6)
        record = {"retry_episode": 6}

        result = rq._advance_qualification_retry_episode(version, record, cap=3)

        assert result["retry_episode"] == 7
        assert result["retry_episode_cap"] == 3
        assert result["retry_episode"] > result["retry_episode_cap"]

    def test_preserves_other_record_fields(self):
        """Non-episode fields in the record are carried through unchanged."""
        version = _make_fake_version()
        record = {
            "retry_episode": 1,
            "status": "pending",
            "candidate_sha": "deadbeef" * 5,
            "audit_task_id": str(uuid.uuid4()),
        }

        result = rq._advance_qualification_retry_episode(version, record, cap=3)

        assert result["status"] == "pending"
        assert result["candidate_sha"] == record["candidate_sha"]
        assert result["audit_task_id"] == record["audit_task_id"]
        assert result["retry_episode"] == 2

    def test_version_attribute_updated(self):
        """version.qualification_retry_episode is updated to the new episode value."""
        version = _make_fake_version(episode=0)
        record = {"retry_episode": 4}

        rq._advance_qualification_retry_episode(version, record, cap=10)

        assert version.qualification_retry_episode == 5

    def test_cap_stored_in_result(self):
        """Different cap values are faithfully stored in the result."""
        for cap in [1, 3, 5, 10]:
            version = _make_fake_version()
            record = {"retry_episode": 0}
            result = rq._advance_qualification_retry_episode(version, record, cap=cap)
            assert result["retry_episode_cap"] == cap, (
                f"cap={cap} must be stored in result, got {result['retry_episode_cap']}"
            )

    def test_multiple_calls_accumulate(self):
        """Three successive calls advance the episode from 0 to 3."""
        version = _make_fake_version()
        record = {"retry_episode": 0}

        for expected_episode in range(1, 4):
            record = rq._advance_qualification_retry_episode(version, record, cap=3)
            assert record["retry_episode"] == expected_episode

        assert version.qualification_retry_episode == 3

    def test_episode_exceeds_cap_after_successive_calls(self):
        """Four calls with cap=3: episode=4 > cap=3.

        This is the regression scenario: successive sub-respawns within a
        single finalize (RL respawn, infra respawn, merge_grade respawn,
        challenge respawn) each call _advance_qualification_retry_episode,
        and the episode accumulates without any enforcement check.
        """
        version = _make_fake_version()
        record = {"retry_episode": 0}

        for _ in range(4):
            record = rq._advance_qualification_retry_episode(version, record, cap=3)

        assert record["retry_episode"] == 4
        assert record["retry_episode_cap"] == 3
        assert record["retry_episode"] > record["retry_episode_cap"], (
            "After 4 advances with cap=3, retry_episode must exceed "
            "retry_episode_cap — the cap is NOT enforced by this function"
        )


# ===========================================================================
# Episode > cap reporting in last_qualification (DB-backed)
# ===========================================================================


@pytest.mark.asyncio
async def test_infra_failure_record_carries_retry_episode_and_cap(db_session):
    """When last_qualification carries retry_episode and retry_episode_cap,
    _handle_infra_failure preserves both in the resulting record so operators
    can read the episode count and cap from the persisted state.

    This is the 'report honestly' contract: even when episode > cap, both
    values survive into last_qualification so the breach is visible.
    """
    from app.release_qualifier_audit import _handle_infra_failure

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Simulate a qual dict where episode already exceeds the declared cap.
    # This is the production scenario: retry_episode=6, retry_episode_cap=3.
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abc123ef" * 5,
        "qualification_attempt_id": str(uuid.uuid4()),
        "retry_episode": 6,
        "retry_episode_cap": 3,
        "consecutive_infra_errors": 0,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = dict(version.last_qualification)
    await _handle_infra_failure(
        db_session,
        version,
        qual,
        "smoketest_task_failed_claude_code_failed",
    )
    await db_session.commit()
    await db_session.refresh(version)

    # The infra_failure record must carry both retry_episode and retry_episode_cap.
    persisted = version.last_qualification
    assert persisted["status"] == "infra_failure"
    assert persisted.get("retry_episode") == 6, (
        "retry_episode must be preserved in last_qualification — "
        f"got {persisted.get('retry_episode')!r}"
    )
    assert persisted.get("retry_episode_cap") == 3, (
        "retry_episode_cap must be preserved in last_qualification — "
        f"got {persisted.get('retry_episode_cap')!r}"
    )
    # Both values are readable: the breach is visible.
    assert persisted["retry_episode"] > persisted["retry_episode_cap"], (
        "The cap breach (retry_episode > retry_episode_cap) must be "
        "readable from last_qualification — this is the honest reporting path"
    )


@pytest.mark.asyncio
async def test_infra_failure_record_without_retry_episode_defaults_to_zero(db_session):
    """When qual has no retry_episode key, _handle_infra_failure defaults it to 0."""
    from app.release_qualifier_audit import _handle_infra_failure

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "def456ab" * 5,
        "qualification_attempt_id": str(uuid.uuid4()),
        "consecutive_infra_errors": 0,
        # no retry_episode or retry_episode_cap
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = dict(version.last_qualification)
    await _handle_infra_failure(db_session, version, qual, "audit_task_failed")
    await db_session.commit()
    await db_session.refresh(version)

    persisted = version.last_qualification
    assert persisted["status"] == "infra_failure"
    # Default value must be 0, not missing.
    assert persisted.get("retry_episode") == 0
    # retry_episode_cap defaults to None when not in qual.
    assert "retry_episode_cap" in persisted


# ===========================================================================
# _redrive_infra_failure_qualification: cap gate is consecutive_infra_errors
# (not retry_episode)
# ===========================================================================


@pytest.mark.asyncio
async def test_redrive_allowed_when_retry_episode_exceeds_cap_but_streak_below(
    db_session, mocker
):
    """When retry_episode > retry_episode_cap but consecutive_infra_errors is
    still below _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS, the sweeper ALLOWS the
    re-drive.

    This confirms that retry_episode is NOT the gate: the gate is the
    consecutive_infra_errors streak.  The retry_episode cap is advisory.
    """
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # retry_episode=6 exceeds retry_episode_cap=3, but consecutive_infra_errors
    # is below _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS — the redrive must NOT be
    # blocked by the episode cap.
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "cafebabe" * 5,
        "error": "audit_task_failed",
        "retry_episode": 6,
        "retry_episode_cap": 3,
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS - 1,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Patch run_qualification to return spawned without hitting the DB/GitHub.
    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is True, (
        "retry_episode > retry_episode_cap must NOT block re-drive; "
        "only consecutive_infra_errors >= cap blocks it"
    )
    mock_run_qual.assert_called_once()


@pytest.mark.asyncio
async def test_redrive_blocked_when_consecutive_infra_errors_at_cap(db_session):
    """When consecutive_infra_errors reaches _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
    the sweeper BLOCKS the re-drive regardless of retry_episode value.

    This is the authoritative cap gate: consecutive_infra_errors, not
    retry_episode.
    """
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "deadbeef" * 5,
        "error": "audit_task_failed",
        # retry_episode is LOW — episode cap is not the issue.
        "retry_episode": 0,
        "retry_episode_cap": 3,
        # consecutive_infra_errors is AT the cap — this is what blocks the redrive.
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is False, (
        "consecutive_infra_errors at cap must block re-drive; "
        f"expected False, got {result!r}"
    )

    # An operator Alert must be raised (first escalation).
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) >= 1, "an operator Alert must be raised when the cap fires"


@pytest.mark.asyncio
async def test_redrive_passes_retry_episode_from_last_qualification(db_session, mocker):
    """When _redrive_infra_failure_qualification fires, it passes the NEXT episode
    number (current + 1) from last_qualification to run_qualification.

    This verifies the episode counter is correctly threaded through the redrive
    path — run_qualification receives retry_episode = last_qual["retry_episode"] + 1.
    """
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    stored_episode = 2
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "f00dcafe" * 5,
        "error": "audit_task_failed",
        "retry_episode": stored_episode,
        "retry_episode_cap": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        "consecutive_infra_errors": 0,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    mock_run_qual.assert_called_once()
    _call_kwargs = mock_run_qual.call_args[1]  # keyword arguments
    assert _call_kwargs.get("retry_episode") == stored_episode + 1, (
        f"run_qualification must receive retry_episode={stored_episode + 1}, "
        f"got {_call_kwargs.get('retry_episode')!r}"
    )
    assert _call_kwargs.get("retry_episode_cap") == _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS, (
        f"run_qualification must receive retry_episode_cap="
        f"{_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS}"
    )


@pytest.mark.asyncio
async def test_redrive_blocked_when_version_not_infra_failure(db_session):
    """_redrive_infra_failure_qualification returns False (no-op) when
    last_qualification.status != 'infra_failure'."""
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "aabbccdd" * 5,
        "blocker": "below_threshold",
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False


@pytest.mark.asyncio
async def test_redrive_blocked_when_version_not_stabilize_phase(db_session):
    """_redrive_infra_failure_qualification returns False when version is not
    in stabilize phase (e.g. already promoted or build phase)."""
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="build")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "11223344" * 5,
        "error": "audit_task_failed",
        "consecutive_infra_errors": 0,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False


# ===========================================================================
# Regression: episode accumulates across successive infra-failure redrives
# (the production scenario: 64 attempts, episode reached 6 vs cap 3)
# ===========================================================================


def test_episode_accumulation_regression():
    """Regression for the production incident: episode advanced to 6 against
    cap=3 after 64 qualification attempts.

    Simulates the episode counter as it would advance across successive
    sweeper redrives + within-finalize sub-respawns, and verifies that:

      1. The counter keeps accumulating (no enforcement in
         _advance_qualification_retry_episode itself).
      2. The cap breach is visible in the returned dict (retry_episode >
         retry_episode_cap) — this is how operators can detect the incident.
    """
    cap = 3
    version = _make_fake_version()
    record = {"retry_episode": 0}

    # Simulate 6 successive advance calls — one per redrive + sub-respawn.
    for step in range(6):
        record = rq._advance_qualification_retry_episode(version, record, cap=cap)

    final_episode = record["retry_episode"]
    final_cap = record["retry_episode_cap"]

    assert final_episode == 6, f"Expected episode=6, got {final_episode}"
    assert final_cap == 3, f"Expected cap=3, got {final_cap}"
    assert final_episode > final_cap, (
        "After 6 advances with cap=3, retry_episode must exceed "
        "retry_episode_cap — the cap breach must be detectable from the "
        "stored dict fields (honest reporting)"
    )
    assert version.qualification_retry_episode == 6
