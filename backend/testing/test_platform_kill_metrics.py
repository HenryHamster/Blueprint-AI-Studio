"""Tests for the platform-kill metric.

The load-bearing tests here are the ones that prove the classifier is NOT
vacuous. A classifier that quietly routes everything it does not recognize to
"not a platform kill" reports a beautiful zero while measuring nothing, which is
worse than having no metric — so the suite asserts on a non-empty corpus at every
point where a bare ``all(...)`` would otherwise pass over an empty list.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.failure_classification import (
    ADMIN_CANCEL,
    AGENT_STALLED,
    AGENT_TIMEOUT,
    ALL_FAILURE_CLASSES,
    APPROVED_CAP_PR_CLOSED,
    CODE_ERROR,
    EXTERNAL_API,
    PROVISION_FAILURE,
    RESIDENT_REPLY_PROTOCOL_FAILURE,
    SCOPE_EMPTY,
    UNPLACEABLE_EXECUTION_SITE,
)
from app.models import TaskStatus
from app.time_utils import _utcnow
from app.platform_kill_metrics import (
    NOT_A_DEATH,
    NOT_A_DEATH_CLASSES,
    PLATFORM_KILL,
    PLATFORM_KILL_CLASSES,
    UNCLASSIFIED,
    WORK_OUTCOME,
    WORK_OUTCOME_CLASSES,
    classify_terminal_task,
    compute_platform_kill_metrics,
)
from app.task_surface_visibility import (
    LEGACY_RESIDENT_SESSION_CONTEXT_KEY,
    RESIDENT_INFRASTRUCTURE_CONTEXT_KEY,
)
from testing.factories import make_membership, make_project, make_task


NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)


def _task(context=None, message=None, completed_at=None, **kw):
    return make_task(
        status=TaskStatus.CANCELLED,
        context=context or {},
        latest_status_message=message,
        completed_at=completed_at or (NOW - timedelta(hours=1)),
        **kw,
    )


# ── the totality guard: the anti-vacuity property ───────────────────────────


class TestClassMapTotality:
    """The three class maps must partition the canonical class universe.

    This is the guard that keeps the metric honest over time. Without it, adding
    a failure class to ``failure_classification`` would silently land in the
    "not a platform kill" bucket forever, and the rate would drift down for
    reasons that have nothing to do with reliability improving.
    """

    def test_corpus_is_non_empty(self):
        """Guard the guard: every assertion below is vacuous on an empty tuple.

        ``assert all(...)`` over an empty list is True. A totality test that
        never actually looks at a class is indistinguishable from one that
        cannot fail, so pin the corpus size before asserting anything over it.
        """
        assert len(ALL_FAILURE_CLASSES) >= 20, (
            "ALL_FAILURE_CLASSES shrank unexpectedly; the totality assertions "
            "below would be running over a near-empty corpus and could pass "
            "without checking anything"
        )
        assert len(PLATFORM_KILL_CLASSES) >= 10
        assert len(WORK_OUTCOME_CLASSES) >= 5
        assert len(NOT_A_DEATH_CLASSES) >= 2

    def test_every_canonical_class_is_triaged(self):
        """No canonical failure class may be missing from all three maps."""
        union = (
            set(PLATFORM_KILL_CLASSES)
            | set(WORK_OUTCOME_CLASSES)
            | set(NOT_A_DEATH_CLASSES)
        )
        missing = sorted(c for c in ALL_FAILURE_CLASSES if c not in union)
        assert missing == [], (
            f"failure classes {missing} are not triaged in "
            "platform_kill_metrics. Each must be added to exactly one of "
            "PLATFORM_KILL_CLASSES / WORK_OUTCOME_CLASSES / NOT_A_DEATH_CLASSES "
            "with a reason, or the platform-kill rate silently under-counts."
        )

    def test_maps_are_pairwise_disjoint(self):
        """A class in two maps would make the verdict depend on lookup order."""
        p, w, n = (
            set(PLATFORM_KILL_CLASSES),
            set(WORK_OUTCOME_CLASSES),
            set(NOT_A_DEATH_CLASSES),
        )
        assert p & w == set(), f"in both platform-kill and work-outcome: {p & w}"
        assert p & n == set(), f"in both platform-kill and not-a-death: {p & n}"
        assert w & n == set(), f"in both work-outcome and not-a-death: {w & n}"

    def test_every_entry_carries_a_reason(self):
        """"Surface reasons, not just states" — a bucket name is not an answer."""
        entries = [
            *PLATFORM_KILL_CLASSES.items(),
            *WORK_OUTCOME_CLASSES.items(),
            *NOT_A_DEATH_CLASSES.items(),
        ]
        assert len(entries) >= 25, "corpus too small for this assertion to mean anything"
        for cls, reason in entries:
            assert isinstance(reason, str) and len(reason) > 20, (
                f"{cls} has no usable reason string; an operator reading the "
                "breakdown would see a bucket name and nothing actionable"
            )


# ── classification ──────────────────────────────────────────────────────────


class TestClassifyTerminalTask:
    @pytest.mark.parametrize(
        "cls",
        [PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API, AGENT_STALLED,
         APPROVED_CAP_PR_CLOSED, UNPLACEABLE_EXECUTION_SITE,
         RESIDENT_REPLY_PROTOCOL_FAILURE],
    )
    def test_platform_classes_count_as_kills_with_a_reason(self, cls):
        v = classify_terminal_task(_task({"failure_category": cls}))
        assert v.verdict == PLATFORM_KILL
        assert v.kill_class == cls
        assert v.evidence_field == "context.failure_category"
        assert v.evidence_value == cls
        assert len(v.reason) > 20

    @pytest.mark.parametrize("cls", [CODE_ERROR, SCOPE_EMPTY])
    def test_work_classes_are_not_kills(self, cls):
        v = classify_terminal_task(_task({"failure_category": cls}))
        assert v.verdict == WORK_OUTCOME
        assert v.kill_class is None

    def test_admin_cancel_is_not_a_death(self):
        v = classify_terminal_task(_task({"failure_category": ADMIN_CANCEL}))
        assert v.verdict == NOT_A_DEATH

    def test_falls_back_to_failure_class_key(self):
        v = classify_terminal_task(_task({"failure_class": PROVISION_FAILURE}))
        assert v.verdict == PLATFORM_KILL
        assert v.evidence_field == "context.failure_class"

    # ── the honest-unknown tests ──

    def test_terminal_task_with_no_recorded_reason_is_unclassified(self):
        """The bucket that keeps the metric honest.

        A terminal row whose terminalizing path recorded nothing must NOT be
        silently counted as "not a platform kill" — it must say so.
        """
        v = classify_terminal_task(_task({}, message="Cancelled: dependency task failed"))
        assert v.verdict == UNCLASSIFIED
        assert v.evidence_field == "none"
        assert "no persisted failure_category" in v.reason

    def test_unknown_persisted_class_is_unclassified_not_silently_ignored(self):
        """A class the maps have never been taught must surface, not vanish.

        This is the regression that makes the totality guard enforceable at
        runtime as well as at test time: if someone adds a new failure class to
        the writers but not to the maps, live rows carrying it land in
        ``unclassified`` where they are visible, rather than quietly deflating
        the platform-kill rate.
        """
        v = classify_terminal_task(_task({"failure_category": "SOME_BRAND_NEW_CLASS"}))
        assert v.verdict == UNCLASSIFIED
        assert v.evidence_value == "SOME_BRAND_NEW_CLASS"
        assert "not triaged" in v.reason

    # ── the "retry is not absolution" regression ──

    def test_supersede_does_not_erase_a_platform_kill(self):
        """A replacement is recovery, not absolution.

        Measured on the live estate: 87 rows carry BOTH
        ``failure_category == AGENT_TIMEOUT`` and
        ``failure_detail == superseded_by_review_pipeline`` — a review agent lost
        its heartbeat and the pipeline spawned a replacement. If the supersede
        decided the verdict, 87 of 133 AGENT_TIMEOUT kills would disappear: the
        platform's own remediation hiding the platform's own defect. The kill is
        still counted; ``work_continued`` records that a replacement ran.
        """
        v = classify_terminal_task(
            _task({
                "failure_category": AGENT_TIMEOUT,
                "failure_detail": "superseded_by_review_pipeline",
            })
        )
        assert v.verdict == PLATFORM_KILL, (
            "a supersede must not erase a platform kill — the attempt still died "
            "of a platform cause"
        )
        assert v.kill_class == AGENT_TIMEOUT
        assert v.work_continued is True

    def test_replaced_by_task_id_does_not_erase_a_platform_kill(self):
        v = classify_terminal_task(
            _task({
                "failure_category": PROVISION_FAILURE,
                "_replaced_by_task_id": str(uuid.uuid4()),
            })
        )
        assert v.verdict == PLATFORM_KILL
        assert v.work_continued is True

    def test_supersede_alone_is_not_a_death(self):
        """With no class to decide the verdict, a supersede does decide it."""
        v = classify_terminal_task(
            _task({"failure_detail": "superseded_by_review_pipeline"})
        )
        assert v.verdict == NOT_A_DEATH
        assert v.work_continued is True

    # ── gate deadlocks recorded as cancels ──

    def test_approval_timeout_is_a_kill_despite_admin_cancel_class(self):
        """failure_detail outranks the class: a gate deadlock is not a cancel."""
        v = classify_terminal_task(
            _task({
                "failure_category": ADMIN_CANCEL,
                "failure_detail": "approval_timeout_preserved_for_handmerge",
            })
        )
        assert v.verdict == PLATFORM_KILL
        assert "approval gate timed out" in v.reason

    # ── narrow message markers ──

    def test_evaluating_parent_stranding_is_a_kill(self):
        v = classify_terminal_task(
            _task({}, message="Cancelled: parent evaluation timeout after 48.1h")
        )
        assert v.verdict == PLATFORM_KILL
        assert v.evidence_field == "latest_status_message"

    def test_unrecognized_message_stays_unclassified(self):
        """The marker list must stay narrow — no keyword heuristics."""
        v = classify_terminal_task(_task({}, message="Cancelled for some new reason"))
        assert v.verdict == UNCLASSIFIED


# ── aggregation ─────────────────────────────────────────────────────────────


class TestComputePlatformKillMetrics:
    def _corpus(self):
        """A deliberately mixed corpus: kills, work outcomes AND unknowns."""
        return [
            _task({"failure_category": AGENT_TIMEOUT},
                  completed_at=NOW - timedelta(days=1)),
            _task({"failure_category": AGENT_TIMEOUT},
                  completed_at=NOW - timedelta(days=1)),
            _task({"failure_category": PROVISION_FAILURE},
                  completed_at=NOW - timedelta(days=2)),
            _task({"failure_category": CODE_ERROR},
                  completed_at=NOW - timedelta(days=1)),
            _task({"failure_category": ADMIN_CANCEL},
                  completed_at=NOW - timedelta(days=1)),
            _task({}, message="Cancelled: dependency task failed",
                  completed_at=NOW - timedelta(days=1)),
            _task({}, message="Cancelled: another mystery",
                  completed_at=NOW - timedelta(days=3)),
            # outside the window — must not be counted
            _task({"failure_category": AGENT_TIMEOUT},
                  completed_at=NOW - timedelta(days=40)),
        ]

    def test_counts_and_rate(self):
        r = compute_platform_kill_metrics(self._corpus(), window_days=7, now=NOW)
        assert r["terminal_task_count"] == 7, "the 40-day-old row must be excluded"
        assert r["platform_kill_count"] == 3
        assert r["platform_kills_per_day"] == round(3 / 7, 3)
        assert r["work_outcome_count"] == 1
        assert r["not_a_death_count"] == 1
        assert r["unclassified_count"] == 2

    def test_corpus_is_not_vacuous(self):
        """Assert the fixture exercises every branch it claims to.

        A breakdown assertion over an empty ``by_class`` list would pass while
        proving nothing. Pin that all four verdicts actually occur.
        """
        r = compute_platform_kill_metrics(self._corpus(), window_days=7, now=NOW)
        assert r["platform_kill_count"] > 0
        assert r["work_outcome_count"] > 0
        assert r["not_a_death_count"] > 0
        assert r["unclassified_count"] > 0, (
            "the unclassified bucket is never exercised by this corpus, so the "
            "tests below cannot prove it works"
        )
        assert len(r["by_class"]) >= 2

    def test_classified_fraction_exposes_an_unmeasured_zero(self):
        """A zero rate must be readable as 'unmeasured' when coverage is poor."""
        unknowns = [
            _task({}, message=f"Cancelled: mystery {i}",
                  completed_at=NOW - timedelta(days=1))
            for i in range(10)
        ]
        r = compute_platform_kill_metrics(unknowns, window_days=7, now=NOW)
        assert r["platform_kills_per_day"] == 0.0
        assert r["unclassified_count"] == 10
        assert r["classified_fraction"] == 0.0, (
            "a zero kill rate with 100% unclassified must report a zero "
            "classified fraction, or an operator would read 'no defects' from "
            "'measured nothing'"
        )

    def test_breakdown_carries_reasons(self):
        r = compute_platform_kill_metrics(self._corpus(), window_days=7, now=NOW)
        assert r["by_class"], "vacuous: no classes to check reasons on"
        for row in r["by_class"]:
            assert row["reason"], f"{row['kill_class']} counted with no reason"
        top = r["by_class"][0]
        assert top["kill_class"] == AGENT_TIMEOUT
        assert top["count"] == 2

    def test_daily_series_is_zero_filled(self):
        r = compute_platform_kill_metrics(self._corpus(), window_days=7, now=NOW)
        assert len(r["daily"]) == 7, "a gap day must appear as 0, not as missing data"
        assert [d["date"] for d in r["daily"]] == sorted(d["date"] for d in r["daily"])
        daily = {row["date"]: row for row in r["daily"]}
        assert daily[(NOW - timedelta(days=1)).date().isoformat()][
            "platform_kills_by_class"
        ] == {AGENT_TIMEOUT: 2}
        assert daily[(NOW - timedelta(days=2)).date().isoformat()][
            "platform_kills_by_class"
        ] == {PROVISION_FAILURE: 1}
        assert daily[NOW.date().isoformat()]["platform_kills_by_class"] == {}
        for row in r["daily"]:
            assert sum(row["platform_kills_by_class"].values()) == row[
                "platform_kills"
            ]

    def test_recovered_kills_are_counted_separately(self):
        tasks = [
            _task({"failure_category": AGENT_TIMEOUT,
                   "failure_detail": "superseded_by_review_pipeline"},
                  completed_at=NOW - timedelta(days=1)),
            _task({"failure_category": AGENT_TIMEOUT},
                  completed_at=NOW - timedelta(days=1)),
        ]
        r = compute_platform_kill_metrics(tasks, window_days=7, now=NOW)
        assert r["platform_kill_count"] == 2
        assert r["platform_kills_recovered"] == 1

    def test_empty_window_reports_none_not_a_fake_zero(self):
        r = compute_platform_kill_metrics([], window_days=7, now=NOW)
        assert r["terminal_task_count"] == 0
        assert r["classified_fraction"] is None, (
            "with no data the classified fraction must be None, not 1.0 — a "
            "fabricated 100% would advertise perfect coverage of nothing"
        )

    def test_rejects_non_positive_window(self):
        with pytest.raises(ValueError):
            compute_platform_kill_metrics([], window_days=0)


# ── endpoint ────────────────────────────────────────────────────────────────


class TestPlatformKillMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_scopes_to_project_and_reports_unknowns(
        self, client: AsyncClient, db_session
    ):
        project_a = make_project()
        project_b = make_project(name="Other Project")
        db_session.add_all([project_a, project_b])
        await db_session.commit()

        # Membership on A only. B is a foreign tenant: its terminal tasks must
        # never reach A's denominator, and asking for B directly must fail
        # closed rather than leak a count.
        db_session.add(make_membership(project_a.id))
        await db_session.commit()

        now = _utcnow()
        recent = now - timedelta(hours=6)

        db_session.add_all([
            make_task(project_id=project_a.id, status=TaskStatus.FAILED,
                      context={"failure_category": AGENT_TIMEOUT},
                      completed_at=recent, updated_at=recent),
            make_task(project_id=project_a.id, status=TaskStatus.CANCELLED,
                      context={"failure_category": EXTERNAL_API},
                      completed_at=recent, updated_at=recent),
            make_task(project_id=project_a.id, status=TaskStatus.CANCELLED,
                      context={"failure_category": CODE_ERROR},
                      completed_at=recent, updated_at=recent),
            # no recorded reason -> must land in unclassified, visibly
            make_task(project_id=project_a.id, status=TaskStatus.CANCELLED,
                      context={}, latest_status_message="Cancelled: mystery",
                      completed_at=recent, updated_at=recent),
            # Resident generations are control-plane audit rows, not project
            # work. Neither the current nor rollout-compatibility marker may
            # enter this metric's denominator or consume its query row cap.
            make_task(project_id=project_a.id, status=TaskStatus.FAILED,
                      context={RESIDENT_INFRASTRUCTURE_CONTEXT_KEY: True,
                               "failure_category": AGENT_TIMEOUT},
                      completed_at=recent, updated_at=recent),
            make_task(project_id=project_a.id, status=TaskStatus.FAILED,
                      context={LEGACY_RESIDENT_SESSION_CONTEXT_KEY: True,
                               "failure_category": AGENT_TIMEOUT},
                      completed_at=recent, updated_at=recent),
            # other project -> excluded
            make_task(project_id=project_b.id, status=TaskStatus.FAILED,
                      context={"failure_category": PROVISION_FAILURE},
                      completed_at=recent, updated_at=recent),
            # still running -> excluded
            make_task(project_id=project_a.id, status=TaskStatus.IN_PROGRESS,
                      context={"failure_category": AGENT_TIMEOUT},
                      completed_at=None, updated_at=recent),
        ])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/platform-kill-metrics?project_id={project_a.id}&days=7"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["project_id"] == str(project_a.id)
        assert body["terminal_task_count"] == 4
        assert body["platform_kill_count"] == 2
        assert body["work_outcome_count"] == 1
        assert body["unclassified_count"] == 1
        assert body["classified_fraction"] == 0.75

        causal_days = [row for row in body["daily"] if row["platform_kills"]]
        assert len(causal_days) == 1
        assert causal_days[0]["platform_kills"] == 2
        assert causal_days[0]["platform_kills_by_class"] == {
            AGENT_TIMEOUT: 1,
            EXTERNAL_API: 1,
        }

        classes = {row["kill_class"] for row in body["by_class"]}
        assert classes == {AGENT_TIMEOUT, EXTERNAL_API}
        for row in body["by_class"]:
            assert row["reason"], "each counted kill must carry WHY"

        assert body["unclassified_reasons"], (
            "an unclassified row must surface a reason explaining what was "
            "missing, not just increment a counter"
        )

    @pytest.mark.asyncio
    async def test_endpoint_fails_closed_for_a_foreign_project(
        self, client: AsyncClient, db_session
    ):
        """A non-member must not learn another tenant's kill count.

        The metric aggregates failure reasons across a whole project, so an
        unscoped read is a disclosure, not just a wrong number. The membership
        boundary is asserted here so a future refactor that drops the
        ``_get_user_project_with_role`` call fails the suite instead of
        silently widening the endpoint.
        """
        foreign = make_project(name="Foreign Project")
        db_session.add(foreign)
        await db_session.commit()

        recent = _utcnow() - timedelta(hours=6)
        db_session.add(
            make_task(
                project_id=foreign.id,
                status=TaskStatus.FAILED,
                context={"failure_category": AGENT_TIMEOUT},
                completed_at=recent,
                updated_at=recent,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/platform-kill-metrics?project_id={foreign.id}&days=7"
        )
        assert resp.status_code == 404
        assert "platform_kill_count" not in resp.text
