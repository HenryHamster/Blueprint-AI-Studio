"""Platform kills per day — the operator's reliability bar, instrumented.

The stated bar is "zero platform defects killing valid work; every platform-
killed task is a defect class owed a root fix; metric: kills/day -> 0". Until
this script existed that metric had no reading: defect classes were found
anecdotally, and the fix queue had no stopping condition because nobody could
say whether the rate was falling.

Usage:
  python scripts/kills_per_day.py --project-id <uuid>
  python scripts/kills_per_day.py --project-id <uuid> --days 14
  python scripts/kills_per_day.py --project-id <uuid> --json
  python scripts/kills_per_day.py --project-id <uuid> --source client
  python scripts/kills_per_day.py --project-id <uuid> --validate-dating 25

--------------------------------------------------------------------------------
ONE TAXONOMY, NOT TWO
--------------------------------------------------------------------------------
This script contains NO failure taxonomy of its own. It imports
``app.platform_kill_metrics`` -- ``classify_terminal_task`` and
``compute_platform_kill_metrics`` -- from the backend package, so the number a
developer reads here and the number ``/api/admin/platform-kill-metrics`` serves
are produced by the same code. A second copy of the class maps would drift from
the first the moment either moved, and a metric that disagrees with itself is
worse than no metric.

That import is also why this script deliberately does not re-derive anything
from log text: the canonical module's ground-truth rule (read persisted per-task
evidence, never re-classify from a log sentence) is inherited wholesale.

--------------------------------------------------------------------------------
TWO SOURCES, AND WHY THE FALLBACK EXISTS
--------------------------------------------------------------------------------
``--source server`` calls ``/api/admin/platform-kill-metrics``. That endpoint
day-buckets on the durable ``Task.completed_at`` column and is the exact answer.

``--source client`` pages the read-only task list and classifies locally. It
exists because the endpoint is not always deployed: it lands on ``automain``
ahead of ``main``, and the control plane serves ``main``. That gap is not
hypothetical -- this script was written across it. The endpoint 404'd when the
first reading was attempted and began answering partway through, when release
v45 promoted to ``main``. Without the fallback the instrument would have been
unreadable for exactly as long as it took the endpoint to promote, which is
precisely the window in which someone wants the first reading.

``--source auto`` (the default) tries the endpoint and falls back on 404.

PREFER THE SERVER PATH WHEN IT ANSWERS. The two were compared on the same
14-day window once both were available, and they did not agree: server 0.571
kills/day over 596 terminal rows, client 0.357 over 146. The client path had
stopped paging early and was reading a biased subset while reporting a healthy
74.7% coverage figure -- a confident, wrong, quiet number, which is the precise
failure this instrument exists to prevent.

The stop bug is fixed (see ``terminal_date``). Re-run afterwards, the client
path read 700 rows instead of 300 and found 220 in-window terminal rows instead
of 146 -- and, still short of the endpoint's 596, correctly printed NOT A RESULT
with its rate labelled (FLOOR) rather than presenting 0.357 as a measurement.
That is the intended end state, not a remaining bug: the fallback reads a
bounded page budget while the endpoint scans the column directly, so it IS a
floor, and it now says so instead of looking like an answer.

--------------------------------------------------------------------------------
WHAT THE CLIENT PATH CANNOT SEE  (read this before trusting a client-path date)
--------------------------------------------------------------------------------
The task-list projection's exposure of ``completed_at`` is version-dependent:
some deployments serve it and some serve only ``created_at``, ``started_at``,
``last_heartbeat`` and ``last_tool_use_at``. When the stamp is absent the client
path cannot know when a task went terminal, so it buckets on the LATEST KNOWN
ACTIVITY timestamp -- a lower bound on the true terminal time. The rendered
``dating`` line always says which rule was actually used for the run in front of
you; never assume.

The proxy was MEASURED against ground truth rather than assumed. Sampling 30
terminal rows on the live estate and recovering each one's true terminal
transition from its ``status_log``:

    20/30 (66.7%) landed in the correct UTC day
    median error   54 minutes
    p90 error      ~20 DAYS
    max error      ~22 days

The median is excellent and the tail is terrible, and the tail is not random: a
task that goes quiet and is cancelled weeks later by a cascade or a bulk stop
carries a last-activity timestamp from the week it ran, not the week it died.

Consequences, stated plainly:

  * the client path's PER-DAY SERIES is unreliable for roughly a third of rows;
  * WINDOW MEMBERSHIP is affected too, in both directions, so the client-path
    total is an estimate rather than a count.

``--validate-dating N`` re-measures this for the run in front of you instead of
trusting the number above: it samples N rows, pulls each one's true terminal
transition out of ``status_log``, and reports the same-day agreement rate. It is
off by default because it costs one extra request per sampled row.

The proper fix is to deploy ``/api/admin/platform-kill-metrics``, or to add
``completed_at`` to the list projection. Both are cheap; neither is this
script's business to do.

Coverage has a second failure mode that looks nothing like the first: rows that
were never READ. A truncated fetch still computes a tidy classified_fraction
over what it managed to read, so it reports healthy coverage and a low rate at
the same time. ``fetch_complete`` is therefore part of the coverage verdict, not
a footnote beside it -- an incomplete fetch prints NOT A RESULT no matter how
well-labelled the rows it did read happened to be.

--------------------------------------------------------------------------------
COVERAGE IS PART OF THE RESULT, NOT A FOOTNOTE
--------------------------------------------------------------------------------
Roughly half of terminal rows in the current estate carry no persisted failure
class, because most terminalizing code paths never stamped one. A metric that
treated an unlabelled row as "not a kill" would report a beautiful, falling,
meaningless number -- the single most dangerous way to be wrong here, because it
looks like success.

Every rendering therefore prints ``classified_fraction`` next to the rate, and
the human output refuses to present the rate as a result when coverage is below
``--coverage-floor`` (default 0.70): it prints NOT A RESULT instead. That is a
presentation guard, not a data change; ``--json`` always carries the raw numbers
and the same ``coverage_sufficient`` flag.

--------------------------------------------------------------------------------
THE DERIVED-CLASS PROBE (secondary, never folded into the headline)
--------------------------------------------------------------------------------
The list projection also carries a TOP-LEVEL ``failure_class`` that the API
derives from log text at read time. It is much denser than the persisted
category, so it is tempting to use it -- and wrong to, because deriving from log
prose is the thing the canonical ground-truth rule forbids.

It is still worth *reporting*, for one specific question an operator will ask
about the unknown bucket: is it hiding kills? This script answers that by
classifying the derived class of otherwise-unclassified rows through the SAME
canonical class maps and reporting the result in a separate ``derived_probe``
section, explicitly labelled as log-derived and excluded from
``platform_kills_per_day``. If the probe says the unknown bucket is mostly
CODE_ERROR, that is evidence the headline is not hiding a mountain of kills --
evidence, not proof, and the output says which it is.

--------------------------------------------------------------------------------
BEING GENTLE WITH THE CONTROL PLANE
--------------------------------------------------------------------------------
The control plane is a self-hosted box. Task rows are fat (tens of KB of
``context`` each) and the list endpoint's serialization cost is super-linear in
page size: measured on the live estate, ``limit=100`` took 43s and then began
returning 502, while ``limit=50`` returned the same rows in 2-4s. The default
page size is therefore 50, pages are fetched sequentially with a pause between
them, and 502/503 responses are retried with backoff rather than hammered.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app.platform_kill_metrics import (  # noqa: E402
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

TERMINAL_STATUSES = ("FAILED", "CANCELLED")

# Ordered most-precise-first for reporting; the LATEST value wins (see
# proxy_terminal_date), because these fields are not ordered in time.
PROXY_DATE_FIELDS = (
    "last_tool_use_at",
    "last_heartbeat",
    "started_at",
    "created_at",
)

DATING_EXACT = "exact:completed_at"
DATING_PROXY = "proxy:last_known_activity"

DEFAULT_COVERAGE_FLOOR = 0.70

# Measured prior, from a 30-row ground-truth sample (see the module docstring).
# Reported when --validate-dating did not run, so a reader is never left to
# assume the proxy is exact.
MEASURED_DATING_SAME_DAY_RATE = 0.667

# "Status changed: IN_PROGRESS -> TaskStatus.FAILED" in a status_log entry.
_TERMINAL_TRANSITION = re.compile(
    r"Status changed:.*?(?:TaskStatus\.)?(FAILED|CANCELLED)\s*$"
)


class KillsPerDayError(RuntimeError):
    """Raised when the reading cannot be produced at all."""


# ── API row adaptation ──────────────────────────────────────────────────────


def _parse_ts(raw: Any) -> datetime | None:
    """Parse an API ISO-8601 timestamp to a naive-UTC datetime."""
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def proxy_terminal_date(row: dict) -> tuple[datetime | None, str | None]:
    """Best available stand-in for when an API-listed task went terminal.

    Returns ``(timestamp, field_name)``. The list projection has no
    ``completed_at``, so this takes the LATEST timestamp among the known
    activity fields -- the last moment the platform observed the task alive,
    which is a lower bound on when it died.

    The latest value is used rather than the first field present because the
    fields are not ordered in time: a task can carry a ``last_heartbeat`` newer
    than its ``last_tool_use_at``, and taking the first present field would then
    date the row too early.
    """
    best: datetime | None = None
    best_field: str | None = None
    for field in PROXY_DATE_FIELDS:
        candidate = _parse_ts(row.get(field))
        if candidate is None:
            continue
        if best is None or candidate > best:
            best, best_field = candidate, field
    return best, best_field


def terminal_date(row: dict) -> tuple[datetime | None, str | None]:
    """When this row went terminal, by the SAME rule the report buckets on.

    Returns ``(timestamp, proxy_field)``; ``proxy_field`` is None when the row
    carried a real stamp and non-None when the activity proxy was used.

    This exists so the paging stop-criterion and the day-bucketing cannot
    disagree. They did once, and the bug is worth recording because its symptom
    was the exact failure mode this whole script is built to prevent. Rows were
    bucketed on a stamped ``completed_at`` while paging stopped on
    ``proxy_terminal_date``, which ignores that stamp. A page of tasks that went
    quiet weeks ago and were only cancelled recently therefore looked entirely
    out-of-window and halted the scan, and the run reported a confidently LOW
    rate (0.357/day against a true 0.571/day) under a healthy-looking 74.7%
    coverage figure. Wrong, and reassuring, and silent.

    Any future date rule must be added here, not at either call site.
    """
    stamped = _parse_ts(row.get("completed_at")) or _parse_ts(row.get("updated_at"))
    if stamped is not None:
        return stamped, None
    return proxy_terminal_date(row)


class ApiTaskRow:
    """Adapts one API task dict to the attribute surface the classifier reads.

    ``compute_platform_kill_metrics`` was written against ORM ``Task`` objects
    and reads ``id`` / ``context`` / ``latest_status_message`` / ``completed_at``
    / ``updated_at`` as attributes. This is the whole adapter; it adds no
    classification logic, and deliberately keeps ``context`` intact rather than
    projecting a subset, because dropping a context key here would silently push
    correctly-labelled rows into the unknown bucket -- a vacuity this script's
    tests pin against.
    """

    __slots__ = (
        "id", "context", "latest_status_message", "completed_at", "updated_at",
        "proxy_field", "derived_failure_class", "status",
    )

    def __init__(self, row: dict):
        self.id = row.get("id")
        context = row.get("context")
        self.context = context if isinstance(context, dict) else {}
        self.latest_status_message = row.get("latest_status_message")
        self.status = row.get("status")
        # The API derives this from log text at read time. Carried for the
        # derived probe ONLY; classify_terminal_task never sees it.
        self.derived_failure_class = row.get("failure_class")
        when, proxy_field = terminal_date(row)
        self.completed_at = when
        self.updated_at = when
        self.proxy_field = proxy_field


def adapt_rows(rows: Iterable[dict]) -> list[ApiTaskRow]:
    return [ApiTaskRow(row) for row in rows]


# ── the derived-class probe ─────────────────────────────────────────────────


def canonical_verdict(failure_class: str) -> str | None:
    """Verdict for a class name, via the canonical maps only.

    Returns ``None`` for a class no map has been taught -- the same honest
    answer the canonical classifier gives, rather than defaulting to "not a
    kill".
    """
    if failure_class in PLATFORM_KILL_CLASSES:
        return PLATFORM_KILL
    if failure_class in WORK_OUTCOME_CLASSES:
        return WORK_OUTCOME
    if failure_class in NOT_A_DEATH_CLASSES:
        return NOT_A_DEATH
    return None


def derived_probe(adapted: Iterable[ApiTaskRow], *, window_start: datetime) -> dict:
    """What the read-time-derived ``failure_class`` says about unknown rows.

    Strictly a secondary diagnostic. It answers "is the unknown bucket hiding
    kills?" and nothing else; its counts never enter ``platform_kill_count``.
    """
    verdicts: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    would_be_kill_ids: list[str] = []
    unknown_rows = 0
    still_unknown = 0

    for task in adapted:
        if task.completed_at is None or task.completed_at.date() < window_start.date():
            continue
        if classify_terminal_task(task).verdict != UNCLASSIFIED:
            continue
        unknown_rows += 1
        derived = task.derived_failure_class
        if not isinstance(derived, str) or not derived:
            still_unknown += 1
            continue
        verdict = canonical_verdict(derived)
        if verdict is None:
            still_unknown += 1
            continue
        verdicts[verdict] += 1
        classes[derived] += 1
        if verdict == PLATFORM_KILL and len(would_be_kill_ids) < 10:
            would_be_kill_ids.append(str(task.id))

    return {
        "note": (
            "SECONDARY, log-derived, excluded from the headline. The API derives "
            "this failure_class from log text at read time; the canonical "
            "ground-truth rule forbids using it to decide the metric. Reported "
            "so the unknown bucket's likely composition is visible."
        ),
        "unclassified_rows_examined": unknown_rows,
        "still_unknown_after_probe": still_unknown,
        "would_be_verdicts": dict(verdicts),
        "would_be_classes": classes.most_common(10),
        "would_be_kill_task_ids": would_be_kill_ids,
    }


# ── dating self-validation ──────────────────────────────────────────────────


def true_terminal_timestamp(status_log: Any) -> datetime | None:
    """Recover the true terminal transition time from a task's ``status_log``.

    The log carries lines like ``Status changed: IN_PROGRESS -> TaskStatus.FAILED``.
    The LAST such line wins: a task can be re-driven and re-terminalized, and the
    final transition is the one that stuck.
    """
    if not isinstance(status_log, list):
        return None
    found: datetime | None = None
    for entry in status_log:
        if not isinstance(entry, dict):
            continue
        if _TERMINAL_TRANSITION.search(str(entry.get("msg") or "")):
            stamp = _parse_ts(entry.get("t"))
            if stamp is not None:
                found = stamp
    return found


def validate_dating(
    api_fn,
    rows: list[dict],
    *,
    sample_size: int,
    sleep_fn=time.sleep,
    pause_seconds: float = 1.0,
    log=lambda msg: None,
) -> dict:
    """Measure the proxy's same-day accuracy against ``status_log`` ground truth.

    Deliberately bounded and opt-in: one extra request per sampled row. Samples
    evenly across the fetched rows rather than taking the newest N, because the
    newest rows are the ones most likely to have died while still active -- the
    easy case -- and sampling only those would flatter the proxy.
    """
    datable = [r for r in rows if proxy_terminal_date(r)[0] is not None]
    if not datable or sample_size <= 0:
        return {"sampled": 0, "note": "no rows available to validate"}

    step = max(1, len(datable) // sample_size)
    sample = datable[::step][:sample_size]

    same_day = 0
    compared = 0
    no_truth = 0
    deltas: list[float] = []
    for row in sample:
        task_id = row.get("id")
        try:
            payload = api_fn("GET", f"/api/admin/tasks/{task_id}", timeout=120)
        except Exception as exc:  # noqa: BLE001 - a sample miss must not fail the run
            log(f"  dating validation: skipped {str(task_id)[:8]} ({exc})")
            sleep_fn(pause_seconds)
            continue
        task = payload.get("task", payload) if isinstance(payload, dict) else payload
        truth = true_terminal_timestamp((task or {}).get("status_log"))
        proxied, _ = proxy_terminal_date(row)
        if truth is None or proxied is None:
            no_truth += 1
        else:
            compared += 1
            deltas.append((truth - proxied).total_seconds())
            if truth.date() == proxied.date():
                same_day += 1
        sleep_fn(pause_seconds)

    deltas.sort()

    def _pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        return values[min(len(values) - 1, int(len(values) * q))]

    return {
        "sampled": len(sample),
        "compared": compared,
        "no_ground_truth": no_truth,
        "same_day": same_day,
        "same_day_rate": round(same_day / compared, 4) if compared else None,
        "median_error_seconds": _pct(deltas, 0.5),
        "p90_error_seconds": _pct(deltas, 0.9),
        "max_error_seconds": deltas[-1] if deltas else None,
        "note": (
            "Ground truth recovered from each sampled task's status_log terminal "
            "transition. same_day_rate is the fraction of rows the proxy placed "
            "in the correct UTC day bucket."
        ),
    }


# ── fetching ────────────────────────────────────────────────────────────────


def _is_status(exc: Exception, code: int) -> bool:
    return f"HTTP {code}" in str(exc)


def fetch_terminal_rows(
    api_fn,
    project_id: str,
    *,
    page_size: int = 50,
    max_rows: int = 1200,
    pause_seconds: float = 1.0,
    sleep_fn=time.sleep,
    oldest_needed: datetime | None = None,
    stale_pages_before_stop: int = 3,
    log=lambda msg: None,
) -> tuple[list[dict], str]:
    """Page the read-only task list for terminal rows. Returns (rows, why_ended).

    ``why_ended`` is one of ``exhausted`` (the list ran out -- the only complete
    result), ``early_stop`` (enough consecutive out-of-window pages) or
    ``max_rows``. Callers must treat the latter two as incomplete: both truncate
    from the OLD end of the window, so both bias the kill count DOWNWARD.

    Sequential, small pages, with a pause between them and backoff on 502/503:
    the control plane is a self-hosted box that has died of resource exhaustion,
    and this script must never be the thing that pushes it over.

    The early stop requires ``stale_pages_before_stop`` CONSECUTIVE fully
    out-of-window pages, not one. The list is ordered by creation and the report
    buckets by termination, so the two orderings are not the same and a single
    old-looking page proves nothing: one long-parked task among recently-created
    ones is enough to make an in-window page look stale. A single-page stop is
    what produced the 0.357-vs-0.571 undercount this module's ``terminal_date``
    docstring records.
    """
    rows: list[dict] = []
    offset = 0
    why_ended = "exhausted"
    stale_streak = 0
    while len(rows) < max_rows:
        want = min(page_size, max_rows - len(rows))
        status_q = "".join(f"&status={s}" for s in TERMINAL_STATUSES)
        path = (
            f"/api/admin/tasks?project_id={project_id}{status_q}"
            f"&limit={want}&offset={offset}"
        )
        batch = None
        for attempt in range(4):
            try:
                payload = api_fn("GET", path, timeout=180)
                batch = (
                    payload.get("tasks", payload)
                    if isinstance(payload, dict)
                    else payload
                )
                break
            except Exception as exc:  # noqa: BLE001 - retried below or re-raised
                if _is_status(exc, 502) or _is_status(exc, 503):
                    delay = 5 * (attempt + 1)
                    log(f"  page offset={offset}: {exc}; retrying in {delay}s")
                    sleep_fn(delay)
                    continue
                raise
        if batch is None:
            raise KillsPerDayError(
                f"task list unavailable at offset={offset} after retries; the "
                "control plane kept returning 502/503"
            )
        if not batch:
            break
        rows.extend(batch)
        log(f"  fetched {len(batch)} rows (offset={offset}, total={len(rows)})")
        if len(batch) < want:
            break
        if oldest_needed is not None:
            page_dates = [terminal_date(r)[0] for r in batch]
            newest = max((d for d in page_dates if d is not None), default=None)
            if newest is not None and newest < oldest_needed:
                stale_streak += 1
                if stale_streak >= stale_pages_before_stop:
                    why_ended = "early_stop"
                    break
            else:
                stale_streak = 0
        offset += len(batch)
        if len(rows) >= max_rows:
            why_ended = "max_rows"
            break
        sleep_fn(pause_seconds)
    return rows, why_ended


# ── report assembly ─────────────────────────────────────────────────────────


def _annotate_coverage(report: dict, coverage_floor: float) -> None:
    """Attach the coverage verdict. Never changes a count -- only labels it.

    Coverage has TWO independent ways to fail, and both bias the rate downward:
    rows that were read but carry no reason, and rows that were never read at
    all. The second is easy to forget because an incomplete fetch still yields a
    tidy classified_fraction over what it did read -- the truncated run that
    prompted this reported 74.7% coverage and a 37%-low rate. So an incomplete
    fetch forces coverage_sufficient False regardless of the fraction.
    """
    fraction = report.get("classified_fraction")
    report["coverage_floor"] = coverage_floor
    fetch_complete = report.get("fetch_complete", True)
    report["coverage_sufficient"] = bool(
        fraction is not None and fraction >= coverage_floor and fetch_complete
    )
    if not fetch_complete:
        report["coverage_verdict"] = (
            "NOT A RESULT: the fetch did not read the whole window "
            f"({report.get('fetch_ended_because')}), so rows are missing from "
            "the OLD end of it and the rate below is a floor, not a "
            "measurement. Raise --max-rows, or use --source server."
        )
        return
    if fraction is None:
        report["coverage_verdict"] = (
            "NO DATA: no terminal tasks in the window, so there is nothing to "
            "classify and no rate to report."
        )
    elif fraction >= coverage_floor:
        report["coverage_verdict"] = (
            f"USABLE: {fraction:.1%} of terminal rows carry a classifiable "
            f"reason, at or above the {coverage_floor:.0%} floor."
        )
    else:
        report["coverage_verdict"] = (
            f"NOT A RESULT: only {fraction:.1%} of terminal rows carry a "
            f"classifiable reason (floor {coverage_floor:.0%}). The rate below "
            "is a floor on the true number, not a measurement -- an unlabelled "
            "row is not a proven non-kill."
        )


def build_client_report(
    rows: list[dict],
    *,
    days: int,
    now: datetime,
    why_ended: str = "exhausted",
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
    project_id: str | None = None,
    dating_validation: dict | None = None,
) -> dict:
    """Classify API rows and assemble the report, annotated with its own limits."""
    adapted = adapt_rows(rows)
    report = compute_platform_kill_metrics(adapted, window_days=days, now=now)

    dated_by_proxy = sum(1 for t in adapted if t.proxy_field is not None)
    proxy_fields = Counter(t.proxy_field for t in adapted if t.proxy_field)
    undatable = sum(1 for t in adapted if t.completed_at is None)

    report["source"] = "client"
    report["project_id"] = project_id
    report["dating"] = DATING_PROXY if dated_by_proxy else DATING_EXACT
    report["dating_note"] = (
        "The deployed task-list projection exposes no completed_at/updated_at, "
        "so rows are bucketed on the latest observed activity timestamp -- a "
        "LOWER BOUND on the true terminal time, not the terminal time itself."
        if dated_by_proxy
        else "Rows carried a stamped completed_at/updated_at; dating is exact."
    )
    report["proxy_fields"] = dict(proxy_fields)
    report["rows_fetched"] = len(rows)
    report["rows_undatable"] = undatable
    report["fetch_ended_because"] = why_ended
    report["fetch_complete"] = why_ended == "exhausted"
    report["dating_validation"] = dating_validation
    if dated_by_proxy and dating_validation is None:
        report["dating_prior_same_day_rate"] = MEASURED_DATING_SAME_DAY_RATE
    window_start = now - timedelta(days=days)
    report["derived_probe"] = derived_probe(adapted, window_start=window_start)
    _annotate_coverage(report, coverage_floor)
    return report


def build_server_report(
    payload: dict,
    *,
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
) -> dict:
    """Wrap the server endpoint's payload in the same reporting envelope."""
    report = dict(payload)
    report["source"] = "server"
    report["dating"] = DATING_EXACT
    report["dating_note"] = (
        "Bucketed server-side on the durable Task.completed_at column."
    )
    report["derived_probe"] = None
    report["dating_validation"] = None
    _annotate_coverage(report, coverage_floor)
    return report


# ── rendering ───────────────────────────────────────────────────────────────


def _bar(count: int, peak: int, width: int = 30) -> str:
    if peak <= 0 or not count:
        return ""
    return "#" * max(1, round(count / peak * width))


def _render_dating(report: dict, w) -> None:
    w(f"  dating       : {report.get('dating')}\n")
    if report.get("dating") != DATING_PROXY:
        return
    w(f"                 {report.get('dating_note')}\n")
    fields = report.get("proxy_fields") or {}
    if fields:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(fields.items()))
        w(f"                 proxy fields: {rendered}\n")
    validation = report.get("dating_validation")
    if validation and validation.get("same_day_rate") is not None:
        rate = validation["same_day_rate"]
        w(f"                 MEASURED this run: {rate:.1%} of "
          f"{validation.get('compared')} sampled rows landed in the correct "
          f"UTC day\n")
        median = validation.get("median_error_seconds")
        p90 = validation.get("p90_error_seconds")
        if median is not None and p90 is not None:
            w(f"                 error: median {median / 3600:.2f}h, "
              f"p90 {p90 / 3600:.2f}h\n")
    else:
        prior = report.get("dating_prior_same_day_rate")
        if prior is not None:
            w(f"                 prior measurement: ~{prior:.1%} of rows land in "
              f"the correct UTC day (re-measure with --validate-dating N)\n")


def render_human(report: dict, out=sys.stdout) -> None:
    """Human-readable rendering. Coverage leads; the rate never stands alone."""
    w = out.write
    days = report.get("window_days")
    w("\n=== PLATFORM KILLS PER DAY ===\n")
    w(f"  project      : {report.get('project_id') or '(all)'}\n")
    w(f"  window       : {days}d from {report.get('window_start')}\n")
    w(f"  source       : {report.get('source')}\n")
    _render_dating(report, w)

    w("\n--- COVERAGE (read this before the rate) ---\n")
    terminal = report.get("terminal_task_count", 0)
    unknown = report.get("unclassified_count", 0)
    fraction = report.get("classified_fraction")
    w(f"  terminal rows in window : {terminal}\n")
    w(f"  classifiable            : {terminal - unknown}\n")
    w(f"  unknown (no reason)     : {unknown}\n")
    w(f"  classified fraction     : "
      f"{'n/a' if fraction is None else f'{fraction:.1%}'}\n")
    w(f"  {report.get('coverage_verdict')}\n")

    w("\n--- RATE ---\n")
    kills = report.get("platform_kill_count", 0)
    label = (
        "platform kills/day"
        if report.get("coverage_sufficient")
        else "platform kills/day (FLOOR)"
    )
    w(f"  {label} : {report.get('platform_kills_per_day', 0.0)}"
      f"   ({kills} kills over {days}d)\n")
    w(f"  of those, retried elsewhere : {report.get('platform_kills_recovered', 0)}\n")
    w(f"  work outcomes (not defects) : {report.get('work_outcome_count', 0)}\n")
    w(f"  not-a-death (stops/retries) : {report.get('not_a_death_count', 0)}\n")

    by_class = report.get("by_class") or []
    w("\n--- BY DEFECT CLASS (the next root fix picks itself) ---\n")
    if not by_class:
        w("  (no platform kills classified in this window)\n")
    for entry in by_class:
        w(f"  {entry.get('count'):4d}  {entry.get('kill_class')}\n")
        w(f"        why: {entry.get('reason')}\n")
        samples = entry.get("sample_task_ids") or []
        if samples:
            w(f"        e.g. {', '.join(str(s)[:8] for s in samples)}\n")

    daily = report.get("daily") or []
    if daily:
        peak = max((d.get("platform_kills", 0) for d in daily), default=0)
        w("\n--- TREND ---\n")
        if report.get("dating") == DATING_PROXY:
            w("  (proxy-dated: day assignment is unreliable for a minority of "
              "rows -- see the dating line above)\n")
        for day in daily:
            count = day.get("platform_kills", 0)
            causes = ", ".join(
                f"{kill_class}={class_count}"
                for kill_class, class_count in sorted(
                    (day.get("platform_kills_by_class") or {}).items()
                )
            )
            cause_suffix = f"  [{causes}]" if causes else ""
            w(
                f"  {day.get('date')}  {count:3d}  {_bar(count, peak)}"
                f"{cause_suffix}\n"
            )

    probe = report.get("derived_probe")
    if probe:
        w("\n--- DERIVED PROBE (secondary; NOT in the rate above) ---\n")
        w(f"  unknown rows examined      : {probe.get('unclassified_rows_examined')}\n")
        w(f"  still unknown after probe  : {probe.get('still_unknown_after_probe')}\n")
        verdicts = probe.get("would_be_verdicts") or {}
        if verdicts:
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items()))
            w(f"  log-derived class suggests : {rendered}\n")
        for cls, count in (probe.get("would_be_classes") or [])[:5]:
            w(f"        {count:4d}  {cls}\n")
        w("  This is evidence about the unknown bucket, not a measurement of it.\n")

    unknown_reasons = report.get("unclassified_reasons") or []
    if unknown_reasons:
        w("\n--- WHY ROWS WERE UNKNOWN ---\n")
        for entry in unknown_reasons[:5]:
            w(f"  {entry.get('count'):4d}  {entry.get('reason')}\n")

    if report.get("fetch_complete") is False:
        w(f"\n  WARNING: the fetch ended early ({report.get('fetch_ended_because')}); "
          "rows at the old end of the window were never read, so every count "
          "above is a floor. Raise --max-rows, or use --source server.\n")
    w("\n")


# ── entrypoint ──────────────────────────────────────────────────────────────


def _resolve_api():
    from blueprint_api import api  # noqa: PLC0415 - keeps import cost off --help

    return api


def run(args: argparse.Namespace, api_fn=None, now: datetime | None = None) -> dict:
    """Produce the report dict. Separated from main() so tests can drive it."""
    api_fn = api_fn or _resolve_api()
    # Naive-UTC, matching the backend's _utcnow() and the naive datetimes the
    # canonical metrics module compares against. datetime.utcnow() is deprecated.
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    project_id = args.project_id
    verbose = not args.json

    def log(message: str) -> None:
        if verbose:
            print(message, file=sys.stderr)

    if args.source in ("auto", "server"):
        query = f"?days={args.days}"
        if project_id:
            query += f"&project_id={project_id}"
        try:
            payload = api_fn("GET", "/api/admin/platform-kill-metrics" + query,
                             timeout=180)
            return build_server_report(payload, coverage_floor=args.coverage_floor)
        except Exception as exc:  # noqa: BLE001
            if args.source == "server" or not _is_status(exc, 404):
                raise
            log(
                "  /api/admin/platform-kill-metrics returned 404 -- the endpoint "
                "is not deployed on this control plane; falling back to "
                "client-side classification over the task list."
            )

    if not project_id:
        raise KillsPerDayError("--project-id is required for the client path")

    slack = timedelta(days=args.days + args.slack_days)
    rows, why_ended = fetch_terminal_rows(
        api_fn,
        project_id,
        page_size=args.page_size,
        max_rows=args.max_rows,
        pause_seconds=args.pause,
        oldest_needed=now - slack,
        log=log,
    )

    validation = None
    if args.validate_dating > 0:
        log(f"  validating dating against status_log on {args.validate_dating} rows")
        validation = validate_dating(
            api_fn, rows, sample_size=args.validate_dating,
            pause_seconds=args.pause, log=log,
        )

    return build_client_report(
        rows,
        days=args.days,
        now=now,
        why_ended=why_ended,
        coverage_floor=args.coverage_floor,
        project_id=project_id,
        dating_validation=validation,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Platform kills per day, by defect class, with honest coverage."
    )
    parser.add_argument("--project-id", default=None, help="Project UUID to measure")
    parser.add_argument("--days", type=int, default=14,
                        help="Window in days (default 14)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument(
        "--source", choices=("auto", "server", "client"), default="auto",
        help="auto (default): try the endpoint, fall back to client-side",
    )
    parser.add_argument(
        "--page-size", type=int, default=50,
        help="Task-list page size (default 50; 100 has been measured to 502 this host)",
    )
    parser.add_argument("--max-rows", type=int, default=2500,
                        help="Fetch cap (default 2500). Hitting it marks the "
                             "reading incomplete rather than silently low.")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="Seconds between requests")
    parser.add_argument(
        "--slack-days", type=int, default=7,
        help="Extra creation-order lookback; the list sorts by creation, not termination",
    )
    parser.add_argument(
        "--coverage-floor", type=float, default=DEFAULT_COVERAGE_FLOOR,
        help="Classified fraction below which the rate is labelled NOT A RESULT",
    )
    parser.add_argument(
        "--validate-dating", type=int, default=0, metavar="N",
        help="Sample N rows and measure proxy dating against status_log ground truth",
    )
    args = parser.parse_args(argv)

    if args.days <= 0:
        parser.error("--days must be positive")

    try:
        report = run(args)
    except KillsPerDayError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        render_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
