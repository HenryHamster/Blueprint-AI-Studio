"""A review-fix park is a BOUNDED window with a real decision at the end.

``review_pipeline._halt_for_cross_episode_abandon_candidate`` and the
stuck/standoff escalation both parked a review-fix chain "for operator
disposition" with NO expiry. ``review_loop_parked_for_operator`` then returned
the park reason forever, every respawn/cap/stale-review path deferred to it,
and the ONLY release was a human calling
``POST /api/admin/tasks/{id}/reset-iteration-cap``. Live evidence on
2026-08-23: three abandon-candidate rows stranded 7h/15h/10h, and standoff row
``a72ca0d6-8813-4215-ba3f-bc51cef82ea9`` (rollup PR #17690) sat EVALUATING 15h+
at head ``627e5748`` — identical to the live branch tip, so the stale-head
release path could never fire either — with its research parent ``b15e2891``
stuck behind it.

That is exactly the banned shape: a gate that revokes autonomous authority and
escalates to a human because a bounded budget overflowed. These tests pin the
replacement — after a bounded window the engine DECIDES (abandon) and
terminalizes — and, just as importantly, they pin the three things that make
deciding autonomously safe:

  * the PR is NEVER closed or merged (lineage ``d3c12745`` / PR #16759,
    2026-08-06: a sweep that took PR authority destroyed a real landed fix
    across 19 burned review tasks);
  * a park with no readable provenance FAILS CLOSED and stays parked;
  * an abandonment never stamps ``resolved`` — that word means the loop
    CONVERGED, and abandoning must not masquerade as convergence.
"""

import copy
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.admin_approval_authority import (
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
)
from app.models import MessageType, Project, ProjectMessage, Task, TaskStatus
from app.review_loop_convergence import (
    REVIEW_LOOP_ABANDON_CANDIDATE_KEY,
    REVIEW_LOOP_PARKING_OUTCOME_KINDS,
    REVIEW_LOOP_TERMINAL_OUTCOME_KEY,
    review_loop_parked_for_operator,
)
from app.review_park_disposition import (
    REVIEW_PARK_ABANDONED_FAILURE_DETAIL,
    REVIEW_PARK_DISPOSED_KEY,
    REVIEW_PARK_DISPOSITION_WINDOW_CONFIG_KEY,
    REVIEW_PARK_DISPOSITION_WINDOW_HOURS,
    resolve_review_park_disposition_seconds,
    sweep_dispose_expired_review_parks,
)
from app.time_utils import _utcnow

pytestmark = pytest.mark.asyncio


def _ago(**kwargs):
    """Naive-UTC timestamp ``kwargs`` in the past (``_utcnow`` strips tzinfo)."""
    return _utcnow() - timedelta(**kwargs)


def _no_pr_authority(mocker):
    """Fail the test if ANYTHING in the sweep closes or merges a PR.

    PR #16759's lesson, enforced rather than asserted in a comment: the
    disposition sweep decides the TASK's budget, never the diff's fate.
    """
    patched = {}
    for module in ("app.github_pulls", "app.github"):
        for fn in ("close_pull_request", "merge_pull_request"):
            patched[f"{module}.{fn}"] = mocker.patch(
                f"{module}.{fn}",
                new_callable=AsyncMock,
                side_effect=AssertionError(
                    f"{module}.{fn} must NEVER be called by the disposition sweep"
                ),
            )
    return patched


def _assert_pr_untouched(patched):
    for name, mock in patched.items():
        assert not mock.called, f"{name} was called — the sweep took PR authority"


async def _project(session, name="Park Disposition") -> Project:
    project = Project(name=f"{name} {uuid.uuid4().hex[:6]}", user_id="user-parkdispo")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


def _abandon_marker(*, recorded_ago_hours, reason="cross_episode_abandon_candidate"):
    marker = {
        "status": "abandon_candidate",
        "reason": reason,
        "threshold": 16,
        "attempts": 18,
        "cycles": 4,
        "episodes": 3,
        "detail": "same three findings re-opened every episode",
    }
    if recorded_ago_hours is not None:
        marker["recorded_at"] = _ago(hours=recorded_ago_hours).isoformat()
    return marker


def _outcome_marker(
    *,
    kind="standoff",
    recorded_ago_hours,
    resolved=False,
    head_sha="627e5748deba71b0985b15beee0589b594f80d3b",
    pr_number=17690,
):
    outcome = {
        "kind": kind,
        "reason": f"{kind}_detected",
        "pr_number": pr_number,
        "iterations": 9,
        "detail": "reviewer and fixer disagree on the same assertion",
    }
    if recorded_ago_hours is not None:
        outcome["recorded_at"] = _ago(hours=recorded_ago_hours).isoformat()
    if resolved:
        outcome["resolved"] = True
        outcome["resolved_by"] = "merge"
    if head_sha is not None:
        outcome["head_sha"] = head_sha
    return outcome


async def _parked_task(
    session,
    project,
    *,
    context,
    status=TaskStatus.EVALUATING,
    requires_approval=False,
) -> Task:
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "review the rollup PR"}],
        status=status,
        requires_approval=requires_approval,
        context=context,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _abandon_context(*, recorded_ago_hours, pr_number=17690, owned_hold=False):
    ctx = {
        "review_loop": {
            "pr_number": pr_number,
            "head_sha": "627e5748deba71b0985b15beee0589b594f80d3b",
            "issue_ledger": {"open": [{"id": "F1"}, {"id": "F2"}]},
        },
        REVIEW_LOOP_ABANDON_CANDIDATE_KEY: _abandon_marker(
            recorded_ago_hours=recorded_ago_hours
        ),
    }
    if owned_hold:
        set_at = _ago(hours=recorded_ago_hours or 1).isoformat()
        episode = "ep-abandon-1"
        ctx["approval_hold_reason"] = "cross_episode_abandon_candidate"
        ctx["_approval_set_at"] = set_at
        ctx[APPROVAL_HOLD_EPISODE_KEY] = episode
        ctx[APPROVAL_HOLD_PROVENANCE_KEY] = {
            "episode": episode,
            "set_at": set_at,
            "reason": "cross_episode_abandon_candidate",
            "owner": "review_pipeline",
        }
    return ctx


def _outcome_context(
    *,
    recorded_ago_hours,
    kind="standoff",
    resolved=False,
    parked_head="627e5748deba71b0985b15beee0589b594f80d3b",
    live_head="627e5748deba71b0985b15beee0589b594f80d3b",
    pr_number=17690,
):
    review_loop = {"pr_number": pr_number, "iteration": 9}
    if live_head is not None:
        review_loop["head_sha"] = live_head
    return {
        "review_loop": review_loop,
        REVIEW_LOOP_TERMINAL_OUTCOME_KEY: _outcome_marker(
            kind=kind,
            recorded_ago_hours=recorded_ago_hours,
            resolved=resolved,
            head_sha=parked_head,
            pr_number=pr_number,
        ),
    }


def _disposition(task, key):
    marker = (task.context or {}).get(key) or {}
    return marker.get(REVIEW_PARK_DISPOSED_KEY)


# ── The window itself: ON in code, retunable, never gate-able off ────────────


def test_disposition_window_defaults_in_code_not_in_config():
    """The window ships ON with a CODE default and cannot be configured off.

    Literal pins on purpose. The whole point of the fix is that no host env
    var, feature flag, or absent runtime-config key can restore the unbounded
    park. There is deliberately NO enable flag: a runtime-config key may only
    RETUNE the window, and a nonsense value must fall back to the code default
    rather than silently disabling the sweep by making the window
    astronomically large.
    """
    assert REVIEW_PARK_DISPOSITION_WINDOW_HOURS == 12
    assert REVIEW_PARK_DISPOSITION_WINDOW_CONFIG_KEY == (
        "review_park_disposition_window_hours"
    )

    assert resolve_review_park_disposition_seconds({}) == 12 * 3600
    assert resolve_review_park_disposition_seconds(None) == 12 * 3600
    assert resolve_review_park_disposition_seconds({"unrelated": 1}) == 12 * 3600

    key = REVIEW_PARK_DISPOSITION_WINDOW_CONFIG_KEY
    # Operator retune inside the sane 1..168h band is honoured...
    assert resolve_review_park_disposition_seconds({key: 1}) == 3600
    assert resolve_review_park_disposition_seconds({key: 168}) == 168 * 3600
    # ...and anything outside it, or unparseable, falls back to the CODE value.
    assert resolve_review_park_disposition_seconds({key: 0}) == 12 * 3600
    assert resolve_review_park_disposition_seconds({key: -1}) == 12 * 3600
    assert resolve_review_park_disposition_seconds({key: 100000}) == 12 * 3600
    assert resolve_review_park_disposition_seconds({key: "never"}) == 12 * 3600
    assert resolve_review_park_disposition_seconds({key: None}) == 12 * 3600
    # There is no boolean kill switch to find.
    assert resolve_review_park_disposition_seconds({key: False}) == 12 * 3600


def test_parking_kinds_are_a_single_shared_constant():
    """The disposition sweep must park on EXACTLY the kinds the park detector
    parks on. Duplicating the literal is how the two drift apart and the sweep
    starts abandoning chains that were never parked (or misses ones that were).
    """
    assert isinstance(REVIEW_LOOP_PARKING_OUTCOME_KINDS, frozenset)
    assert sorted(REVIEW_LOOP_PARKING_OUTCOME_KINDS) == ["standoff", "stuck"]


# ── Shape 1: the cross-episode abandon-candidate marker ──────────────────────


async def test_young_abandon_candidate_park_is_not_disposed(db_session, mocker):
    """Inside the window the operator still owns it — nothing is decided."""
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=3)
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert (task.context or {}).get("failure_detail") is None
    assert _disposition(task, REVIEW_LOOP_ABANDON_CANDIDATE_KEY) is None
    # Still parked for the operator, exactly as before.
    assert review_loop_parked_for_operator(task) == "cross_episode_abandon_candidate"
    _assert_pr_untouched(patched)


async def test_expired_abandon_candidate_park_is_abandoned(db_session, mocker):
    """THE DEFECT, INVERTED. Past the window the engine DECIDES and terminalizes.

    The decision is recorded as a breadcrumb nested inside the marker that
    caused the park (so the reason travels with its evidence), the rest of the
    marker is preserved as the audit trail, and the operator-visible status
    message states both the DECISION and the WHY.
    """
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=15)
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert (task.context or {})["failure_detail"] == REVIEW_PARK_ABANDONED_FAILURE_DETAIL

    marker = (task.context or {})[REVIEW_LOOP_ABANDON_CANDIDATE_KEY]
    # The evidence that produced the park survives the decision.
    assert marker["attempts"] == 18
    assert marker["cycles"] == 4
    assert marker["reason"] == "cross_episode_abandon_candidate"

    disposition = marker[REVIEW_PARK_DISPOSED_KEY]
    assert disposition["decided"] == "abandon"
    assert disposition["by"] == "autonomous_disposition"
    assert disposition["window_hours"] == 12
    assert disposition["at"]
    why = disposition["why"]
    assert "did not converge" in why
    assert "18 attempt(s)" in why
    assert "2 finding(s) still open" in why

    msg = (task.latest_status_message or "").lower()
    assert "abandoned" in msg, "the status message must state the DECISION"
    assert "did not converge" in msg, "the status message must state the WHY"
    assert "pr left open" in msg, "the status message must say the PR survives"
    assert "#17690" in (task.latest_status_message or "")
    _assert_pr_untouched(patched)


async def test_disposition_emits_exactly_one_deduped_operator_note(db_session, mocker):
    """One NOTE per disposed task, and a second sweep must not re-emit it."""
    _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=15)
    )

    await sweep_dispose_expired_review_parks(db_session, _utcnow())

    rows = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key == f"review-park-disposed:{task.id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    note = rows[0]
    assert note.message_type == MessageType.NOTE
    assert "abandoned by autonomous disposition" in note.content
    assert "left OPEN" in note.content
    assert (note.context or {}).get("park_shape") == "abandon_candidate"


async def test_double_sweep_is_idempotent(db_session, mocker):
    """A disposed park is never re-decided: the second pass changes nothing."""
    _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=15)
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1
    await db_session.refresh(task)
    first = dict(_disposition(task, REVIEW_LOOP_ABANDON_CANDIDATE_KEY))
    first_message = task.latest_status_message

    # A disposed row is FAILED so it leaves the candidate set. Re-open it to
    # prove the nested breadcrumb ALONE is also sufficient to skip it.
    task.status = TaskStatus.EVALUATING
    db_session.add(task)
    await db_session.commit()

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(task)
    assert _disposition(task, REVIEW_LOOP_ABANDON_CANDIDATE_KEY) == first
    assert task.latest_status_message == first_message
    notes = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key == f"review-park-disposed:{task.id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(notes) == 1


@pytest.mark.parametrize(
    "bad_recorded_at",
    [None, "", "   ", "not-a-timestamp", 1755950000, {"at": "yesterday"}],
    ids=["missing", "empty", "blank", "garbage", "epoch-int", "dict"],
)
async def test_park_without_readable_provenance_stays_parked(
    db_session, mocker, bad_recorded_at
):
    """FAIL CLOSED. A park whose ``recorded_at`` cannot be read is NOT expired.

    Treating an unreadable stamp as "parked since the epoch" would abandon
    every legacy row on the first sweep. This is the same reasoning
    ``review_loop_park_stale_head`` applies to a park with no ``head_sha``.
    """
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    ctx = _abandon_context(recorded_ago_hours=None)
    if bad_recorded_at is not None:
        ctx[REVIEW_LOOP_ABANDON_CANDIDATE_KEY]["recorded_at"] = bad_recorded_at
    task = await _parked_task(db_session, project, context=ctx)

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert (task.context or {}).get("failure_detail") is None
    assert _disposition(task, REVIEW_LOOP_ABANDON_CANDIDATE_KEY) is None
    _assert_pr_untouched(patched)


async def test_owned_abandon_hold_is_released(db_session, mocker):
    """When the hold is provably this sweep's, it is consumed with the decision.

    Leaving ``requires_approval`` TRUE on a terminalized task would leave a
    second, invisible operator gate behind the one just closed.
    """
    _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_abandon_context(recorded_ago_hours=15, owned_hold=True),
        requires_approval=True,
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is False
    ctx = task.context or {}
    assert ctx.get("approval_hold_reason") is None
    assert ctx.get(APPROVAL_HOLD_EPISODE_KEY) is None
    assert ctx.get(APPROVAL_HOLD_PROVENANCE_KEY) is None


async def test_unowned_hold_is_left_alone_but_task_still_terminalizes(
    db_session, mocker
):
    """A newer INDEPENDENT hold is not this sweep's to consume.

    The boolean latch is shared; the provenance is not. The park is still
    decided (that is our authority) but the unrelated hold survives untouched.
    """
    _no_pr_authority(mocker)
    project = await _project(db_session)
    ctx = _abandon_context(recorded_ago_hours=15, owned_hold=True)
    # A later spend-budget hold displaced the abandon-candidate episode.
    ctx["approval_hold_reason"] = "spend_budget_exceeded"
    ctx[APPROVAL_HOLD_PROVENANCE_KEY] = {
        "episode": "ep-spend-9",
        "set_at": ctx["_approval_set_at"],
        "reason": "spend_budget_exceeded",
        "owner": "spawn_calibration",
    }
    ctx[APPROVAL_HOLD_EPISODE_KEY] = "ep-spend-9"
    task = await _parked_task(
        db_session, project, context=ctx, requires_approval=True
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is True, "an unowned hold must survive"
    assert (task.context or {}).get("approval_hold_reason") == "spend_budget_exceeded"
    assert (task.context or {})[APPROVAL_HOLD_EPISODE_KEY] == "ep-spend-9"


# ── Shape 2: an unresolved stuck/standoff terminal outcome ───────────────────


async def test_expired_unresolved_standoff_is_abandoned(db_session, mocker):
    """The live 2026-08-23 row: task a72ca0d6 / PR #17690, standoff, 15h+.

    ``requires_approval`` was FALSE (discretionary gates off), so there was no
    hold to release — the normal path for this shape, not an error — and the
    stale-head release path could never fire because the parked head equalled
    the live branch tip. Nothing but a human could move it. Now the window
    decides it.
    """
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_outcome_context(recorded_ago_hours=15, kind="standoff"),
        requires_approval=False,
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert (task.context or {})["failure_detail"] == REVIEW_PARK_ABANDONED_FAILURE_DETAIL

    outcome = (task.context or {})[REVIEW_LOOP_TERMINAL_OUTCOME_KEY]
    assert outcome["kind"] == "standoff", "the park evidence survives the decision"
    disposition = outcome[REVIEW_PARK_DISPOSED_KEY]
    assert disposition["decided"] == "abandon"
    assert disposition["by"] == "autonomous_disposition"
    why = disposition["why"]
    assert "standoff" in why, "the WHY must name the park kind"
    assert "reviewer and fixer disagree" in why, "the WHY must carry the detail"
    assert "9 iteration(s)" in why
    assert task.requires_approval is False
    _assert_pr_untouched(patched)


async def test_young_unresolved_stuck_park_is_not_disposed(db_session, mocker):
    """Inside the window a stuck park is still the operator's to decide."""
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_outcome_context(recorded_ago_hours=2, kind="stuck"),
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert _disposition(task, REVIEW_LOOP_TERMINAL_OUTCOME_KEY) is None
    assert review_loop_parked_for_operator(task) == "stuck"
    _assert_pr_untouched(patched)


async def test_resolved_terminal_outcome_is_never_disposed(db_session, mocker):
    """``resolved: True`` means a later APPROVE or merge proved the loop healthy.

    Age is irrelevant: this chain is not parked at all, and abandoning it would
    terminalize converged work.
    """
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_outcome_context(recorded_ago_hours=800, resolved=True),
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert (task.context or {}).get("failure_detail") is None
    assert _disposition(task, REVIEW_LOOP_TERMINAL_OUTCOME_KEY) is None
    assert review_loop_parked_for_operator(task) is None
    _assert_pr_untouched(patched)


@pytest.mark.parametrize(
    "kind", ["repeat_fingerprint_halt", "supersession_churn_halt", "cap_exhausted"]
)
async def test_non_parking_outcome_kind_is_never_disposed(db_session, mocker, kind):
    """Only the kinds that actually STOP the loop are parks.

    repeat_fingerprint_halt / supersession_churn_halt raise a releasable hold
    but the loop keeps running, and cap_exhausted terminalizes through its own
    path. Disposing any of them would be abandoning a live chain.
    """
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_outcome_context(recorded_ago_hours=800, kind=kind),
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert _disposition(task, REVIEW_LOOP_TERMINAL_OUTCOME_KEY) is None
    _assert_pr_untouched(patched)


async def test_stale_head_takes_precedence_over_an_expired_window(db_session, mocker):
    """A park recorded against an OLDER head is stale, not expired.

    A differing head proves a commit landed that this park never reviewed —
    that is the existing stale-head RELEASE path's row, and disposing it would
    abandon work that already landed (PR #16759's exact lesson). The comparison
    is made from persisted state only; the sweep never fetches from GitHub.
    """
    patched = _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_outcome_context(
            recorded_ago_hours=40,
            parked_head="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            live_head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ),
    )
    before = copy.deepcopy(task.context)

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert task.context == before, "a stale-head row must be left byte-identical"
    _assert_pr_untouched(patched)


async def test_matching_head_expired_park_is_disposed(db_session, mocker):
    """Control for the stale-head test: same age, heads EQUAL, so it IS decided.

    Without this pair the stale-head test would also pass if the sweep simply
    never disposed anything of that age.
    """
    _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_outcome_context(
            recorded_ago_hours=40,
            parked_head="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            live_head="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


async def test_missing_head_provenance_does_not_block_an_expired_park(
    db_session, mocker
):
    """Staleness that cannot be PROVEN does not veto the expiry decision.

    A legacy park with no recorded head (or a chain with no live head) cannot
    be shown stale. The window already gave the operator their chance, so the
    row is decided rather than parked forever on absent evidence.
    """
    _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context=_outcome_context(
            recorded_ago_hours=40, parked_head=None, live_head=None
        ),
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


async def test_disposition_never_claims_the_loop_converged(db_session, mocker):
    """An abandonment must NOT stamp ``resolved``.

    ``resolved: True`` is written by ``clear_review_escalation_approval_gate``
    and means the loop demonstrably CONVERGED — a later APPROVE or merge proved
    the chain healthy. Abandoning is the opposite finding. If the disposition
    set it, every ``resolved``-keyed reader (park detection, digest outcome
    attribution, convergence metrics) would count a give-up as a success.
    """
    _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session, project, context=_outcome_context(recorded_ago_hours=15)
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(task)
    outcome = (task.context or {})[REVIEW_LOOP_TERMINAL_OUTCOME_KEY]
    assert outcome[REVIEW_PARK_DISPOSED_KEY]["decided"] == "abandon"
    assert "resolved" not in outcome
    assert "resolved_by" not in outcome


async def test_owned_stuck_hold_is_released_with_the_decision(db_session, mocker):
    """When the escalation DID raise a hold bound to this outcome, it is consumed."""
    _no_pr_authority(mocker)
    project = await _project(db_session)
    ctx = _outcome_context(recorded_ago_hours=15, kind="stuck")
    set_at = _ago(hours=15).isoformat()
    episode = "ep-stuck-7"
    provenance = {
        "episode": episode,
        "set_at": set_at,
        "reason": "stuck",
        "owner": "review_loop_convergence",
    }
    ctx["approval_hold_reason"] = "stuck"
    ctx["_approval_set_at"] = set_at
    ctx[APPROVAL_HOLD_EPISODE_KEY] = episode
    ctx[APPROVAL_HOLD_PROVENANCE_KEY] = provenance
    ctx[REVIEW_LOOP_TERMINAL_OUTCOME_KEY]["approval_hold_episode"] = {
        "episode": episode,
        "set_at": set_at,
        "reason": "stuck",
        "provenance": dict(provenance),
    }
    task = await _parked_task(
        db_session, project, context=ctx, requires_approval=True
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is False
    out_ctx = task.context or {}
    assert out_ctx.get("approval_hold_reason") is None
    assert out_ctx.get(APPROVAL_HOLD_EPISODE_KEY) is None
    assert out_ctx.get(APPROVAL_HOLD_PROVENANCE_KEY) is None
    # The breadcrumb the decision is stamped into must SURVIVE the hold release.
    assert out_ctx[REVIEW_LOOP_TERMINAL_OUTCOME_KEY][REVIEW_PARK_DISPOSED_KEY]


async def test_unrelated_spend_hold_survives_a_terminal_outcome_disposition(
    db_session, mocker
):
    """A spend-budget hold is explicitly excluded from this sweep's authority."""
    from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON

    _no_pr_authority(mocker)
    project = await _project(db_session)
    ctx = _outcome_context(recorded_ago_hours=15)
    set_at = _ago(hours=1).isoformat()
    ctx["approval_hold_reason"] = SPEND_BUDGET_APPROVAL_HOLD_REASON
    ctx["_approval_set_at"] = set_at
    ctx[APPROVAL_HOLD_EPISODE_KEY] = "ep-spend-3"
    ctx[APPROVAL_HOLD_PROVENANCE_KEY] = {
        "episode": "ep-spend-3",
        "set_at": set_at,
        "reason": SPEND_BUDGET_APPROVAL_HOLD_REASON,
        "owner": "spawn_calibration",
    }
    task = await _parked_task(
        db_session, project, context=ctx, requires_approval=True
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is True
    assert (task.context or {})["approval_hold_reason"] == (
        SPEND_BUDGET_APPROVAL_HOLD_REASON
    )


# ── Selection boundaries ─────────────────────────────────────────────────────


async def test_already_terminal_and_archived_rows_are_left_alone(db_session, mocker):
    """Nothing terminal or archived is re-decided by the sweep."""
    _no_pr_authority(mocker)
    project = await _project(db_session)
    done = await _parked_task(
        db_session,
        project,
        context=_abandon_context(recorded_ago_hours=40),
        status=TaskStatus.COMPLETED,
    )
    archived = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=40)
    )
    archived.archived = True
    db_session.add(archived)
    await db_session.commit()

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0

    await db_session.refresh(done)
    await db_session.refresh(archived)
    assert done.status == TaskStatus.COMPLETED
    assert _disposition(done, REVIEW_LOOP_ABANDON_CANDIDATE_KEY) is None
    assert _disposition(archived, REVIEW_LOOP_ABANDON_CANDIDATE_KEY) is None


async def test_task_with_no_park_marker_is_untouched(db_session, mocker):
    """An ordinary long-running review task is not a park."""
    _no_pr_authority(mocker)
    project = await _project(db_session)
    task = await _parked_task(
        db_session,
        project,
        context={"review_loop": {"pr_number": 17690, "iteration": 3}},
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 0
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING


async def test_sweep_decides_every_expired_park_in_one_pass(db_session, mocker):
    """Both shapes, mixed with rows that must not move, in a single sweep."""
    _no_pr_authority(mocker)
    project = await _project(db_session)
    old_abandon = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=15)
    )
    old_standoff = await _parked_task(
        db_session, project, context=_outcome_context(recorded_ago_hours=20)
    )
    young = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=1)
    )
    resolved = await _parked_task(
        db_session,
        project,
        context=_outcome_context(recorded_ago_hours=800, resolved=True),
    )

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 2

    for task, expected in (
        (old_abandon, TaskStatus.FAILED),
        (old_standoff, TaskStatus.FAILED),
        (young, TaskStatus.EVALUATING),
        (resolved, TaskStatus.EVALUATING),
    ):
        await db_session.refresh(task)
        assert task.status == expected


async def test_a_poison_row_does_not_lose_the_rest_of_the_sweep(db_session, mocker):
    """One row that blows up must not strand every other expired park.

    A sweep that dies on its first bad row is how a single corrupt task
    re-creates the very forever-park this module exists to end.
    """
    _no_pr_authority(mocker)
    project = await _project(db_session)
    poison = await _parked_task(
        db_session, project, context=_abandon_context(recorded_ago_hours=15)
    )
    healthy = await _parked_task(
        db_session, project, context=_outcome_context(recorded_ago_hours=20)
    )

    from app import review_park_disposition as mod

    real_dispose = mod._dispose_one
    # Snapshot the id: after the poison row's rollback the ORM object is
    # expired and touching an attribute would emit IO of its own.
    poison_id = poison.id

    async def _boom(session, task_id, project_id, **kwargs):
        if task_id == poison_id:
            raise RuntimeError("simulated poison row")
        return await real_dispose(session, task_id, project_id, **kwargs)

    mocker.patch.object(mod, "_dispose_one", side_effect=_boom)

    assert await sweep_dispose_expired_review_parks(db_session, _utcnow()) == 1

    await db_session.refresh(healthy)
    assert healthy.status == TaskStatus.FAILED


# ── Downstream consumers of the new terminal marker ──────────────────────────


def test_park_abandoned_counts_as_a_genuine_rejection():
    """An abandoned park CONCLUDED the loop, so the closed-PR reconciler must
    treat it as a genuine rejection rather than an infrastructure artefact to
    be retried.
    """
    from app.rollup_management import (
        _GENUINE_REJECTION_CTX_MARKERS,
        _ctx_has_genuine_rejection_marker,
    )

    assert REVIEW_PARK_ABANDONED_FAILURE_DETAIL in _GENUINE_REJECTION_CTX_MARKERS
    assert _ctx_has_genuine_rejection_marker(
        {"failure_detail": REVIEW_PARK_ABANDONED_FAILURE_DETAIL}
    )
    assert not _ctx_has_genuine_rejection_marker({"failure_detail": "agent_timeout"})


def test_park_abandoned_is_review_quality_not_a_code_defect():
    """Taxonomy: the loop failed to converge; the subtask did not introduce a bug.

    Without this the disposition sweep's rows fall through to the
    ``code_defect`` default and every abandoned park is reported as a fresh
    code defect — inflating exactly the metric the fix is meant to deflate.
    """
    from app.failure_classification import (
        TAXONOMY_REVIEW_QUALITY,
        classify_failure_taxonomy,
    )

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "fix", "prompt": "fix it"}],
        status=TaskStatus.FAILED,
        latest_status_message="PR #17690 abandoned by autonomous disposition",
        context={"failure_detail": REVIEW_PARK_ABANDONED_FAILURE_DETAIL},
    )
    assert classify_failure_taxonomy(task) == TAXONOMY_REVIEW_QUALITY


async def test_requeue_skips_a_park_abandoned_child_and_says_the_pr_is_open(
    db_session, mocker, caplog
):
    """The parent requeue must NOT clone an abandoned park.

    Unlike ``review_cap_exhausted`` the PR is still OPEN, so a clone would open
    a SECOND PR for work that already has one — and would re-enter the exact
    review-fix loop that just provably failed to converge.
    """
    import logging

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = await _project(db_session, name="Requeue Park")
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "parent"}],
        status=TaskStatus.EVALUATING,
        context={},
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "child work"}],
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        attempt_count=1,
        latest_status_message="PR #17690 abandoned by autonomous disposition",
        context={
            "failure_detail": REVIEW_PARK_ABANDONED_FAILURE_DETAIL,
            REVIEW_LOOP_TERMINAL_OUTCOME_KEY: {
                "kind": "standoff",
                "recorded_at": _ago(hours=20).isoformat(),
                REVIEW_PARK_DISPOSED_KEY: {
                    "decided": "abandon",
                    "by": "autonomous_disposition",
                    "why": "review-fix loop parked at standoff and did not converge",
                },
            },
        },
    )
    db_session.add(child)
    await db_session.commit()

    from app.task_lifecycle_requeue import _requeue_parent_task

    with caplog.at_level(logging.INFO, logger="app.task_lifecycle_requeue"):
        await _requeue_parent_task(parent, db_session)
    await db_session.commit()

    clones = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.id != child.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert clones == [], "an abandoned park must never be cloned"
    text = "\n".join(r.message for r in caplog.records)
    assert REVIEW_PARK_ABANDONED_FAILURE_DETAIL in text
    assert "still OPEN" in text, (
        "the skip log must not reuse the cap-exhausted 'PR already closed' wording"
    )
