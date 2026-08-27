"""Tests for ``scripts/kills_per_day.py``.

The whole value of this instrument is that it separates a PLATFORM KILL (valid
work the platform destroyed) from a LEGITIMATE FAILURE (work that genuinely did
not work). A tool that confuses the two would send the operator hunting root
fixes for agents writing bad code, and would report a falling defect rate while
the defects stayed. So the fixtures below are deliberately paired: each assertion
pins a separation, not just a bucket name.

The second thing these tests defend is the honest unknown. The most dangerous
possible bug here is silent: an adapter that drops ``context`` on the floor makes
every row unclassifiable, and an unclassifiable row that is quietly treated as
"not a kill" produces a beautiful zero. ``test_adapter_actually_delivers_context``
and ``test_unknown_rows_are_not_silently_counted_as_non_kills`` exist to make
that failure mode loud.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import kills_per_day as kpd  # noqa: E402

NOW = datetime(2026, 8, 25, 12, 0, 0)
PROJECT = "37d445e1-0775-4403-bacf-1cd7ed129336"


# ── fixtures shaped like real API rows ──────────────────────────────────────


def _row(task_id: str, *, context=None, message=None, status="FAILED",
         when=None, derived=None, **extra) -> dict:
    """One task dict shaped like the live ``/api/admin/tasks`` projection.

    Note what is ABSENT: no ``completed_at`` and no ``updated_at``. The deployed
    projection does not expose them, and the tests must exercise the same
    impoverished shape the script actually meets.
    """
    stamp = (when or NOW).isoformat()
    row = {
        "id": task_id,
        "status": status,
        "context": context if context is not None else {},
        "latest_status_message": message,
        "created_at": stamp,
        "last_heartbeat": stamp,
        "failure_class": derived,
    }
    row.update(extra)
    return row


def PLATFORM_KILL_ROW(task_id="aaaaaaaa-0000-4000-8000-000000000001", when=None):
    """A VM/agent timeout: the attempt was ended by a clock, not by an assertion."""
    return _row(task_id, context={"failure_category": "AGENT_TIMEOUT"}, when=when)


def LEGITIMATE_FAILURE_ROW(task_id="bbbbbbbb-0000-4000-8000-000000000002", when=None):
    """The contrast class: the candidate's own code and tests failed on merit."""
    return _row(task_id, context={"failure_category": "CODE_ERROR"}, when=when)


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        project_id=PROJECT, days=14, json=True, source="auto", page_size=50,
        max_rows=1200, pause=0.0, slack_days=7,
        coverage_floor=kpd.DEFAULT_COVERAGE_FLOOR, validate_dating=0,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _report(rows, **kwargs):
    kwargs.setdefault("days", 14)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("project_id", PROJECT)
    return kpd.build_client_report(rows, **kwargs)


# ── the separation this tool exists to make ─────────────────────────────────


def test_platform_kill_and_legitimate_failure_are_separated():
    """The core contract: a clock-kill counts, a genuine code failure does not."""
    report = _report([PLATFORM_KILL_ROW(), LEGITIMATE_FAILURE_ROW()])

    assert report["terminal_task_count"] == 2
    assert report["platform_kill_count"] == 1
    assert report["work_outcome_count"] == 1
    assert report["unclassified_count"] == 0

    classes = {entry["kill_class"] for entry in report["by_class"]}
    assert classes == {"AGENT_TIMEOUT"}, (
        "only the timeout may be attributed as a platform defect class"
    )
    kill_samples = report["by_class"][0]["sample_task_ids"]
    assert kill_samples == ["aaaaaaaa-0000-4000-8000-000000000001"]
    assert "bbbbbbbb-0000-4000-8000-000000000002" not in kill_samples, (
        "a legitimate code failure must never appear as a kill sample"
    )


def test_legitimate_failures_alone_produce_a_zero_rate():
    """Ten real code failures are not a platform defect. The rate must be 0."""
    rows = [
        LEGITIMATE_FAILURE_ROW(f"cccccccc-0000-4000-8000-{i:012d}")
        for i in range(10)
    ]
    report = _report(rows)

    assert report["platform_kill_count"] == 0
    assert report["platform_kills_per_day"] == 0.0
    assert report["work_outcome_count"] == 10
    # And the zero is trustworthy precisely because coverage is total.
    assert report["classified_fraction"] == 1.0
    assert report["coverage_sufficient"] is True


def test_deliberate_cancellation_is_not_a_kill():
    """An operator stopping a task is the system working, not a defect."""
    report = _report([_row("dddddddd-0000-4000-8000-000000000004",
                           context={"failure_category": "ADMIN_CANCEL"},
                           status="CANCELLED")])

    assert report["platform_kill_count"] == 0
    assert report["not_a_death_count"] == 1


def test_kills_per_day_divides_by_the_window_not_the_row_count():
    """Seven kills over a 14-day window is 0.5/day, not 7."""
    rows = [
        PLATFORM_KILL_ROW(f"eeeeeeee-0000-4000-8000-{i:012d}",
                          when=NOW - timedelta(days=i))
        for i in range(7)
    ]
    report = _report(rows)

    assert report["platform_kill_count"] == 7
    assert report["platform_kills_per_day"] == 0.5


def test_rows_outside_the_window_are_excluded():
    """A kill from 40 days ago must not inflate a 14-day reading."""
    report = _report([
        PLATFORM_KILL_ROW("ffffffff-0000-4000-8000-000000000006"),
        PLATFORM_KILL_ROW("ffffffff-0000-4000-8000-000000000007",
                          when=NOW - timedelta(days=40)),
    ])

    assert report["terminal_task_count"] == 1
    assert report["platform_kill_count"] == 1


# ── the honest unknown ──────────────────────────────────────────────────────


def test_unknown_rows_are_not_silently_counted_as_non_kills():
    """An unlabelled row must land in `unclassified`, and must drag coverage down.

    This is the falsely-improving-trend guard. If an unlabelled row were folded
    into "not a kill", the rate would look identical to a genuinely clean window
    while measuring nothing.
    """
    rows = [PLATFORM_KILL_ROW()] + [
        _row(f"99999999-0000-4000-8000-{i:012d}") for i in range(9)
    ]
    report = _report(rows)

    assert report["terminal_task_count"] == 10
    assert report["unclassified_count"] == 9
    assert report["classified_fraction"] == 0.1
    assert report["coverage_sufficient"] is False
    assert "NOT A RESULT" in report["coverage_verdict"]
    # The counted kill is still counted -- coverage labels the rate, never edits it.
    assert report["platform_kill_count"] == 1


def test_full_coverage_is_reported_as_usable():
    report = _report([PLATFORM_KILL_ROW(), LEGITIMATE_FAILURE_ROW()])

    assert report["classified_fraction"] == 1.0
    assert report["coverage_sufficient"] is True
    assert "USABLE" in report["coverage_verdict"]


def test_coverage_floor_is_configurable_and_pinned_at_seventy_percent():
    assert kpd.DEFAULT_COVERAGE_FLOOR == 0.70

    rows = [PLATFORM_KILL_ROW()] + [
        _row(f"88888888-0000-4000-8000-{i:012d}") for i in range(3)
    ]
    strict = _report(rows, coverage_floor=0.9)
    lax = _report(rows, coverage_floor=0.2)

    assert strict["coverage_sufficient"] is False
    assert lax["coverage_sufficient"] is True
    # Same underlying counts either way; only the label moved.
    assert strict["platform_kill_count"] == lax["platform_kill_count"] == 1


def test_empty_window_reports_no_data_rather_than_a_confident_zero():
    report = _report([])

    assert report["terminal_task_count"] == 0
    assert report["classified_fraction"] is None
    assert report["coverage_sufficient"] is False
    assert "NO DATA" in report["coverage_verdict"]


# ── the adapter, which is where a silent zero would come from ───────────────


def test_adapter_actually_delivers_context_to_the_classifier():
    """Anti-vacuity: prove the classification came from the row's own context.

    If ``ApiTaskRow`` stopped passing ``context`` through, every test above that
    asserts a *positive* classification would fail -- but a test that only
    checked "kills == 0" would still pass. This one pins the wiring directly.
    """
    adapted = kpd.adapt_rows([PLATFORM_KILL_ROW()])
    assert adapted[0].context == {"failure_category": "AGENT_TIMEOUT"}

    from app.platform_kill_metrics import classify_terminal_task

    verdict = classify_terminal_task(adapted[0])
    assert verdict.verdict == "platform_kill"
    assert verdict.kill_class == "AGENT_TIMEOUT"
    assert verdict.evidence_field == "context.failure_category", (
        "the verdict must be traceable to the exact persisted key it was read from"
    )


def test_adapter_tolerates_a_null_context_without_inventing_a_verdict():
    adapted = kpd.adapt_rows([_row("77777777-0000-4000-8000-000000000009",
                                   context=None)])
    assert adapted[0].context == {}

    report = _report([_row("77777777-0000-4000-8000-000000000009", context=None)])
    assert report["unclassified_count"] == 1
    assert report["platform_kill_count"] == 0


def test_status_message_marker_is_honoured_through_the_adapter():
    """A classless row whose orchestrator marker names a gate deadlock is a kill."""
    report = _report([_row(
        "66666666-0000-4000-8000-000000000010",
        message="CI wait backstop timeout after 6h; reaping task",
    )])

    assert report["platform_kill_count"] == 1
    assert report["unclassified_count"] == 0


# ── the derived probe must never inflate the headline ───────────────────────


def test_derived_failure_class_never_enters_the_headline_rate():
    """A log-derived class is evidence about the unknown bucket, not a kill count.

    The row below carries NO persisted category -- only the top-level
    ``failure_class`` the API derives from log text at read time. It must stay
    ``unclassified`` in the headline and appear only in ``derived_probe``.
    """
    report = _report([_row("55555555-0000-4000-8000-000000000011",
                           derived="AGENT_TIMEOUT")])

    assert report["platform_kill_count"] == 0, (
        "a log-derived class must not be counted as a ground-truth kill"
    )
    assert report["unclassified_count"] == 1

    probe = report["derived_probe"]
    assert probe["unclassified_rows_examined"] == 1
    assert probe["would_be_verdicts"] == {"platform_kill": 1}
    assert probe["would_be_kill_task_ids"] == ["55555555-0000-4000-8000-000000000011"]


def test_derived_probe_reports_work_outcomes_in_the_unknown_bucket():
    """The operator's real question: is the unknown bucket hiding kills?"""
    rows = [
        _row(f"44444444-0000-4000-8000-{i:012d}", derived="CODE_ERROR")
        for i in range(5
        )
    ]
    report = _report(rows)

    probe = report["derived_probe"]
    assert probe["would_be_verdicts"] == {"work_outcome": 5}
    assert probe["would_be_kill_task_ids"] == []
    assert report["platform_kill_count"] == 0


def test_derived_probe_ignores_rows_that_already_have_ground_truth():
    """The probe describes the UNKNOWN bucket only.

    This row carries a persisted CODE_ERROR (a legitimate failure, already
    classified) and, separately, a log-derived AGENT_TIMEOUT. If the probe
    examined already-classified rows it would report a hidden platform kill that
    the ground truth has already ruled out -- manufacturing exactly the alarm the
    probe exists to rule out.
    """
    report = _report([_row(
        "32323232-0000-4000-8000-000000000017",
        context={"failure_category": "CODE_ERROR"},
        derived="AGENT_TIMEOUT",
    )])

    assert report["work_outcome_count"] == 1
    assert report["unclassified_count"] == 0

    probe = report["derived_probe"]
    assert probe["unclassified_rows_examined"] == 0, (
        "the probe must not look at rows the canonical classifier already decided"
    )
    assert probe["would_be_verdicts"] == {}
    assert probe["would_be_kill_task_ids"] == []


def test_derived_probe_leaves_untriaged_classes_unknown():
    """A class no canonical map has been taught stays unknown, not "not a kill"."""
    report = _report([_row("33333333-0000-4000-8000-000000000013",
                           derived="SOME_CLASS_NOBODY_TRIAGED")])

    probe = report["derived_probe"]
    assert probe["still_unknown_after_probe"] == 1
    assert probe["would_be_verdicts"] == {}


def test_canonical_verdict_uses_the_shared_maps_only():
    """No second taxonomy: every mapping resolves through app.platform_kill_metrics."""
    from app.platform_kill_metrics import (
        NOT_A_DEATH_CLASSES,
        PLATFORM_KILL_CLASSES,
        WORK_OUTCOME_CLASSES,
    )

    assert PLATFORM_KILL_CLASSES, "corpus must be non-empty or this is vacuous"
    for name in PLATFORM_KILL_CLASSES:
        assert kpd.canonical_verdict(name) == "platform_kill"
    for name in WORK_OUTCOME_CLASSES:
        assert kpd.canonical_verdict(name) == "work_outcome"
    for name in NOT_A_DEATH_CLASSES:
        assert kpd.canonical_verdict(name) == "not_a_death"
    assert kpd.canonical_verdict("NOT_A_REAL_CLASS") is None


# ── dating: the client path's other honest limit ────────────────────────────


def test_proxy_date_takes_the_latest_activity_not_the_first_field_present():
    """The activity fields are not ordered in time; taking the first dates too early."""
    row = _row("22222222-0000-4000-8000-000000000014")
    row["last_tool_use_at"] = "2026-08-20T01:00:00"
    row["last_heartbeat"] = "2026-08-24T09:30:00"
    row["started_at"] = "2026-08-19T00:00:00"
    row["created_at"] = "2026-08-18T00:00:00"

    stamp, field = kpd.proxy_terminal_date(row)
    assert stamp == datetime(2026, 8, 24, 9, 30, 0)
    assert field == "last_heartbeat"


def test_client_report_declares_its_dating_is_a_proxy():
    report = _report([PLATFORM_KILL_ROW()])

    assert report["dating"] == "proxy:last_known_activity"
    assert "LOWER BOUND" in report["dating_note"]
    assert report["dating_prior_same_day_rate"] == kpd.MEASURED_DATING_SAME_DAY_RATE
    assert report["proxy_fields"], "the report must name which fields supplied dates"


def test_a_stamped_completed_at_is_used_exactly_when_present():
    """Post-deploy shape: once the projection carries completed_at, stop proxying."""
    row = PLATFORM_KILL_ROW()
    row["completed_at"] = "2026-08-24T09:30:00"

    adapted = kpd.adapt_rows([row])
    assert adapted[0].completed_at == datetime(2026, 8, 24, 9, 30, 0)
    assert adapted[0].proxy_field is None

    report = _report([row])
    assert report["dating"] == "exact:completed_at"
    assert "dating_prior_same_day_rate" not in report


def test_true_terminal_timestamp_takes_the_last_transition():
    """A re-driven task terminalizes more than once; the final transition stuck."""
    log = [
        {"t": "2026-08-20T01:00:00", "msg": "Status changed: QUEUED → TaskStatus.FAILED"},
        {"t": "2026-08-20T02:00:00", "msg": "Status changed: QUEUED → TaskStatus.IN_PROGRESS"},
        {"t": "2026-08-24T09:30:00", "msg": "Status changed: IN_PROGRESS → TaskStatus.CANCELLED"},
    ]
    assert kpd.true_terminal_timestamp(log) == datetime(2026, 8, 24, 9, 30, 0)


def test_true_terminal_timestamp_returns_none_without_a_transition():
    assert kpd.true_terminal_timestamp([{"t": "2026-08-20T01:00:00",
                                         "msg": "Using tool: Bash"}]) is None
    assert kpd.true_terminal_timestamp(None) is None


def test_validate_dating_measures_same_day_agreement():
    # created_at precedes the heartbeat, as it always does on a real row.
    row = _row("11111111-0000-4000-8000-000000000015")
    row["created_at"] = "2026-08-24T09:00:00"
    row["last_heartbeat"] = "2026-08-24T09:30:00"
    off_row = _row("11111111-0000-4000-8000-000000000016")
    off_row["created_at"] = "2026-08-01T09:00:00"
    off_row["last_heartbeat"] = "2026-08-01T09:30:00"

    truths = {
        "11111111-0000-4000-8000-000000000015": "2026-08-24T10:00:00",
        "11111111-0000-4000-8000-000000000016": "2026-08-22T10:00:00",
    }

    def fake_api(method, path, timeout=None):
        task_id = path.rsplit("/", 1)[-1]
        return {"task": {"status_log": [
            {"t": truths[task_id], "msg": "Status changed: X → TaskStatus.FAILED"},
        ]}}

    result = kpd.validate_dating(fake_api, [row, off_row], sample_size=2,
                                 sleep_fn=lambda _s: None, pause_seconds=0.0)

    assert result["compared"] == 2
    assert result["same_day"] == 1
    assert result["same_day_rate"] == 0.5


# ── fetching: gentleness is a correctness property here ─────────────────────


def test_fetch_retries_a_502_rather_than_giving_up():
    calls = {"n": 0}

    def flaky_api(method, path, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 502: Bad Gateway")
        return {"tasks": [PLATFORM_KILL_ROW()]}

    rows, why_ended = kpd.fetch_terminal_rows(
        flaky_api, PROJECT, page_size=50, sleep_fn=lambda _s: None,
    )
    assert len(rows) == 1
    assert why_ended == "exhausted"
    assert calls["n"] == 2


def test_fetch_does_not_retry_a_non_transient_error():
    def denied_api(method, path, timeout=None):
        raise RuntimeError("HTTP 403: Forbidden")

    with pytest.raises(RuntimeError, match="403"):
        kpd.fetch_terminal_rows(denied_api, PROJECT, sleep_fn=lambda _s: None)


def test_terminal_date_prefers_the_real_stamp_over_the_activity_proxy():
    """The paging stop and the day-bucketing must read the SAME date.

    A row whose activity went quiet weeks before it was cancelled is the exact
    shape that broke this: dating used the stamp, paging used the proxy, and the
    scan stopped on pages that were only stale by the proxy's reckoning.
    """
    quiet_then_cancelled = _row(
        "d0000000-0000-4000-8000-000000000004",
        when=NOW - timedelta(days=30),          # last activity: long ago
        completed_at=(NOW - timedelta(hours=2)).isoformat(),  # died: just now
    )

    stamped, proxy_field = kpd.terminal_date(quiet_then_cancelled)
    assert proxy_field is None, "a stamped row must not be proxy-dated"
    assert stamped == NOW - timedelta(hours=2)

    # And the proxy alone would have dated it 30 days earlier -- out of window.
    proxied, _ = kpd.proxy_terminal_date(quiet_then_cancelled)
    assert proxied == NOW - timedelta(days=30)

    # The adapter the classifier sees must agree with terminal_date, not proxy.
    adapted = kpd.adapt_rows([quiet_then_cancelled])[0]
    assert adapted.completed_at == NOW - timedelta(hours=2)


def _pager(pages):
    """An api_fn that serves ``pages`` in order, then empties. Counts calls."""
    seen = {"n": 0}

    def paging_api(method, path, timeout=None):
        index = seen["n"]
        seen["n"] += 1
        return {"tasks": pages[index] if index < len(pages) else []}

    return paging_api, seen


def test_fetch_stops_after_enough_consecutive_out_of_window_pages():
    pages = [[_row(f"page0-{i}", when=NOW) for i in range(2)]]
    for page in (1, 2, 3):
        pages.append([_row(f"page{page}-{i}", when=NOW - timedelta(days=60))
                      for i in range(2)])
    paging_api, seen = _pager(pages)

    rows, why_ended = kpd.fetch_terminal_rows(
        paging_api, PROJECT, page_size=2, sleep_fn=lambda _s: None,
        oldest_needed=NOW - timedelta(days=21), stale_pages_before_stop=3,
    )
    assert why_ended == "early_stop"
    assert seen["n"] == 4, "one fresh page then exactly three stale ones"
    assert len(rows) == 8


def test_fetch_does_not_stop_on_a_single_stale_page():
    """A lone old-looking page must not end the scan.

    The list is ordered by creation and the report buckets by termination, so an
    in-window page can look stale. Stopping on the first one is what produced the
    0.357-vs-0.571 undercount against the server endpoint.
    """
    pages = [
        [_row("fresh-0", when=NOW)],
        [_row("stale-0", when=NOW - timedelta(days=60))],
        [PLATFORM_KILL_ROW("cccccccc-0000-4000-8000-000000000003", when=NOW)],
    ]
    paging_api, seen = _pager(pages)

    rows, why_ended = kpd.fetch_terminal_rows(
        paging_api, PROJECT, page_size=1, sleep_fn=lambda _s: None,
        oldest_needed=NOW - timedelta(days=21), stale_pages_before_stop=3,
    )
    assert why_ended == "exhausted"
    assert len(rows) == 3, "the in-window kill after the stale page must be read"
    assert any(r["id"].startswith("cccccccc") for r in rows)


def test_fetch_marks_truncation_when_the_cap_is_hit():
    def endless_api(method, path, timeout=None):
        return {"tasks": [_row(f"x-{i}") for i in range(2)]}

    rows, why_ended = kpd.fetch_terminal_rows(
        endless_api, PROJECT, page_size=2, max_rows=4, sleep_fn=lambda _s: None,
    )
    assert len(rows) == 4
    assert why_ended == "max_rows"

    report = _report(rows, why_ended=why_ended)
    assert report["fetch_complete"] is False
    assert report["fetch_ended_because"] == "max_rows"


def test_an_incomplete_fetch_is_never_reported_as_a_usable_result():
    """Unread rows bias the rate down exactly like unlabelled ones do.

    A truncated fetch still computes a tidy classified_fraction over what it did
    read, so without this guard the run reports healthy coverage and a low rate
    at the same time -- which is what the live client path actually did.
    """
    rows = [PLATFORM_KILL_ROW(f"aaaaaaaa-0000-4000-8000-00000000000{i}")
            for i in range(1, 4)]

    complete = _report(rows, why_ended="exhausted")
    assert complete["classified_fraction"] == 1.0
    assert complete["coverage_sufficient"] is True

    for ended in ("max_rows", "early_stop"):
        partial = _report(rows, why_ended=ended)
        # Same rows, same perfect labelling -- only the fetch differs.
        assert partial["classified_fraction"] == 1.0
        assert partial["coverage_sufficient"] is False, ended
        assert "NOT A RESULT" in partial["coverage_verdict"], ended


def test_default_page_size_stays_at_the_measured_safe_value():
    """limit=100 was measured to take 43s and then 502 on this control plane."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        kpd.main(["--help"])
    assert "default 50" in buf.getvalue()


# ── source selection ────────────────────────────────────────────────────────


def test_auto_source_falls_back_to_the_client_path_on_404():
    """The endpoint lands on automain before main; the fallback keeps it readable."""
    calls = []

    def api(method, path, timeout=None):
        calls.append(path)
        if "platform-kill-metrics" in path:
            raise RuntimeError('HTTP 404: {"detail":"Not Found"}')
        return {"tasks": [PLATFORM_KILL_ROW()] if len(calls) == 2 else []}

    report = kpd.run(_args(source="auto"), api_fn=api, now=NOW)

    assert report["source"] == "client"
    assert report["platform_kill_count"] == 1
    assert any("platform-kill-metrics" in p for p in calls)


def test_explicit_server_source_does_not_silently_fall_back():
    def api(method, path, timeout=None):
        raise RuntimeError('HTTP 404: {"detail":"Not Found"}')

    with pytest.raises(RuntimeError, match="404"):
        kpd.run(_args(source="server"), api_fn=api, now=NOW)


def test_server_payload_is_wrapped_with_the_same_coverage_envelope():
    payload = {
        "window_days": 14, "window_start": "2026-08-11",
        "terminal_task_count": 100, "platform_kill_count": 4,
        "platform_kills_per_day": 0.286, "unclassified_count": 60,
        "classified_fraction": 0.4, "by_class": [], "daily": [],
    }

    def api(method, path, timeout=None):
        return payload

    report = kpd.run(_args(source="server"), api_fn=api, now=NOW)

    assert report["source"] == "server"
    assert report["dating"] == "exact:completed_at"
    assert report["coverage_sufficient"] is False
    assert "NOT A RESULT" in report["coverage_verdict"]
    assert report["derived_probe"] is None


def test_client_path_requires_a_project_id():
    def api(method, path, timeout=None):
        raise RuntimeError('HTTP 404: {"detail":"Not Found"}')

    with pytest.raises(kpd.KillsPerDayError, match="project-id"):
        kpd.run(_args(source="auto", project_id=None), api_fn=api, now=NOW)


# ── rendering ───────────────────────────────────────────────────────────────


def test_human_output_states_coverage_before_the_rate():
    """Ordering is the point: a rate read without its coverage is a false result."""
    import io

    rows = [PLATFORM_KILL_ROW()] + [
        _row(f"aaaa0000-0000-4000-8000-{i:012d}") for i in range(9)
    ]
    buf = io.StringIO()
    kpd.render_human(_report(rows), out=buf)
    text = buf.getvalue()

    assert text.index("COVERAGE") < text.index("--- RATE ---")
    assert "NOT A RESULT" in text
    assert "(FLOOR)" in text, "a sub-floor rate must be labelled as a floor"
    assert "AGENT_TIMEOUT" in text
    assert "[AGENT_TIMEOUT=1]" in text
    assert "proxy-dated" in text, "the trend must warn that day assignment is proxied"


def test_human_output_names_a_representative_task_per_class():
    """A class breakdown a human cannot drill into is not actionable."""
    import io

    buf = io.StringIO()
    kpd.render_human(_report([PLATFORM_KILL_ROW()]), out=buf)

    assert "aaaaaaaa" in buf.getvalue()
