"""Orchestrator liveness heartbeat + hard-watchdog machinery extracted from
``app.orchestrator``.

The orchestrator's liveness *gauges* — the ``_orchestrator_last_cycle`` /
``_orchestrator_cycle_started`` / ``_orchestrator_last_progress`` timestamps —
remain owned by ``app.orchestrator``: ``orchestrator_worker`` mutates them
in-place under ``global`` (a source invariant AST-scans for those bare-name
stamps), and several sibling modules and tests read/write them as
``app.orchestrator.<name>``. This module hosts the pure LOGIC that operates on
those gauges — the progress-heartbeat setter, the bounded post-cycle step
runner, the single-source liveness predicate shared by ``/health`` and the
in-process watchdog, and the out-of-loop OS-thread hard watchdog. The two
functions that touch the gauges reach them through a lazy
``from app import orchestrator`` so the gauges stay single-homed while this
cold, independently-tested logic lives in its own module.

Re-exports from ``app.orchestrator`` keep existing callers
(``from app.orchestrator import evaluate_orchestrator_liveness``, the
``app.orchestrator.<name>`` test hooks, …) working unchanged.
"""

import asyncio
import faulthandler
import logging
import os
import sys
import threading
from datetime import datetime
from typing import NamedTuple, Tuple

from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


# Per-step heartbeat gate: how long the worker may go without a progress
# heartbeat (_mark_orchestrator_progress) before /health reports it as wedged
# (→ Cloud Run liveness restart). This is NOT a full-cycle timeout — a long
# cycle with many sequential CI polls is healthy as long as each individual step
# heartbeats within this window. Must comfortably exceed the longest single
# awaited step (a CI GitHub call + its retries, the 30s session-acquire
# timeout, etc.) so a slow-but-progressing worker is never killed — only a
# truly hung one. Env-overridable for ops tuning.
#
# 180 s (tightened from 300 s): the reduction is a liveness tightening — a
# genuinely stuck step is caught and abandoned ~2 min sooner. It is safe
# because LEADER_STEP_BUDGET_SECONDS (90 s) bounds each monitoring step to
# well under half this threshold, and the dispatch loop heartbeats once per
# CI-poll task (also far below 180 s under normal load). REAPER_HANG_SECONDS
# remains 300 s (the original value) so the two workers are independently tuned.
ORCHESTRATOR_HANG_SECONDS = int(os.getenv("ORCHESTRATOR_HANG_SECONDS", "180"))


def _mark_orchestrator_progress() -> None:
    """Heartbeat that the orchestrator worker is actively progressing within a
    cycle. Liveness (`/health`) is gated on the freshness of this timestamp, not
    on full-cycle completion, so a legitimately long cycle (large EVALUATING
    backlog → many sequential CI polls) is not mistaken for a hung process and
    killed — which would discard the cycle's progress and spiral. See
    [[reference-orchestrator-liveness-deathspiral]] / incident 2026-05-28.

    The gauge stays owned by ``app.orchestrator`` (see module docstring); this
    setter writes it there so every reader — the re-exported name in
    ``orchestrator_worker`` and the sibling modules importing it — sees one
    value."""
    from app import orchestrator as _orch

    _orch._orchestrator_last_progress = _utcnow()


# Default per-step budget for the post-cycle monitoring/idle tail. Overridable via
# env for ops tuning. Kept comfortably below ORCHESTRATOR_HANG_SECONDS so the
# worst-case gap between two heartbeats (one fully-consumed step) can never by
# itself trip the liveness watchdog.
LEADER_STEP_BUDGET_SECONDS = float(os.getenv("LEADER_STEP_BUDGET_SECONDS", "90"))


async def _bounded_leader_step(label: str, coro, budget: float | None = None) -> None:
    """Run one post-cycle monitoring/sweep step bounded by ``budget`` seconds and
    pump the orchestrator progress heartbeat afterward.

    The monitoring + idle tail (``_check_external_health``, the recovery sweeps,
    ``_check_idle_research``/``_check_evergreen_fill`` …) runs AFTER the dispatch
    cycle's last heartbeat and previously had NO heartbeat of its own. A single
    un-bounded await there — slow GitHub/DB I/O, a pool-starved sweep waiting on a
    connection — froze ``_orchestrator_last_progress`` even though dispatch was
    healthy, so ``/health`` flipped unhealthy and Cloud Run liveness-killed a
    still-progressing leader (incident 2026-06-15: post-commit relock cascade on a
    single hot task starved the pool; the tail wedged → 503 → instance kill).

    This bounds each step and heartbeats after it, mirroring the per-task heartbeat
    in the CI-poll loop and the per-tick ``asyncio.timeout`` in
    ``_evaluating_timeout_service``. Liveness in the tail is therefore decoupled
    from any single step: a long-but-progressing tail stays alive, and a genuinely
    hung step is abandoned-and-logged (next cycle retries) instead of wedging.

    Scope: this bounds an *awaitable* hang only. A full event-loop block (e.g. a
    synchronous CPU stall, or every pool connection checked out so even
    ``asyncio.timeout``'s own callback can't be scheduled) cannot be interrupted
    from within the loop and remains the watchdog/Cloud Run backstop's job.

    ``TimeoutError`` is abandon-and-continue. Other exceptions propagate to the
    caller's existing handler so its consecutive-error backoff is preserved. The
    heartbeat is pumped in ``finally`` so it fires on success, timeout, or error.
    """
    if budget is None:
        budget = LEADER_STEP_BUDGET_SECONDS
    try:
        async with asyncio.timeout(budget):
            await coro
    except TimeoutError:
        logger.warning(
            "[ORCHESTRATOR] leader monitoring step '%s' exceeded %.0fs budget — "
            "abandoned this tick (heartbeat pumped; next cycle retries). A step "
            "that times out every cycle is a stuck dependency, not a slow one.",
            label,
            budget,
        )
    finally:
        _mark_orchestrator_progress()


class OrchestratorLiveness(NamedTuple):
    """Result of :func:`evaluate_orchestrator_liveness`.

    - ``alive``:        a liveness signal (progress heartbeat or completed
                        cycle) has been recorded at least once.
    - ``healthy``:      ``alive`` AND the freshest signal is within
                        ``ORCHESTRATOR_HANG_SECONDS`` (the ``/health`` verdict).
    - ``stalled``:      the in-process watchdog should cancel-and-restart — a
                        definite hang, including a first cycle that started but
                        never made progress past the threshold.
    - ``progress_age``: age in seconds of the freshest (progress|cycle) signal,
                        or ``None`` if neither has been recorded yet.
    - ``reason``:       human-readable cause when ``stalled`` (empty otherwise).
    """

    alive: bool
    healthy: bool
    stalled: bool
    progress_age: float | None
    reason: str


def evaluate_orchestrator_liveness(
    now: datetime | None = None,
    *,
    threshold: float | None = None,
) -> "OrchestratorLiveness":
    """Single source of truth for orchestrator liveness.

    Shared by the HTTP ``/health`` endpoint and the in-process background-task
    watchdog so the two judgments can never drift apart again. Liveness is gated
    on the in-cycle progress *heartbeat* (``_orchestrator_last_progress``),
    falling back to the last completed cycle (``_orchestrator_last_cycle``),
    against ``threshold`` (default ``ORCHESTRATOR_HANG_SECONDS``). A legitimately
    long-but-progressing cycle is therefore healthy; only a worker that has
    emitted no progress for longer than the threshold is hung.

    ``threshold`` is overridable so the out-of-loop hard watchdog can reuse this
    exact signal logic (cold-start / first-cycle-never-progressed cases included)
    with its own, larger bound — without forking the predicate. Existing callers
    (``/health``, the in-loop watchdog) pass nothing and keep
    ``ORCHESTRATOR_HANG_SECONDS``.

    Both call sites previously duplicated the signal + threshold logic — the
    watchdog drifted to full-cycle-completion + a hardcoded 120s and started
    killing healthy-but-slow cycles in a restart loop (incident 2026-05-28; only
    ``/health`` was fixed in PR #6678). This helper is the de-duplicated fix.

    The gauges it reads stay owned by ``app.orchestrator`` (see module
    docstring); they are reached through a lazy import so a write to
    ``app.orchestrator._orchestrator_last_progress`` (worker stamp, test hook)
    is observed here.
    """
    from app import orchestrator as _orch

    if now is None:
        now = _utcnow()
    if threshold is None:
        threshold = ORCHESTRATOR_HANG_SECONDS

    last = _orch._orchestrator_last_progress or _orch._orchestrator_last_cycle
    if last is not None:
        age = (now - last).total_seconds()
        if age < threshold:
            return OrchestratorLiveness(True, True, False, age, "")
        return OrchestratorLiveness(
            True, False, True, age, f"no progress heartbeat in {int(age)}s"
        )

    # Cold start: no progress/cycle recorded yet. If a cycle has *started* but
    # never reached its first heartbeat within the threshold, that is a wedged
    # first cycle the watchdog must recover.
    if _orch._orchestrator_cycle_started is not None:
        age = (now - _orch._orchestrator_cycle_started).total_seconds()
        if age >= threshold:
            return OrchestratorLiveness(
                False,
                False,
                True,
                None,
                f"first cycle made no progress in {int(age)}s "
                "(started, never heartbeat)",
            )

    # Nothing recorded yet, or still inside the startup window — not alive yet,
    # but not a hang the watchdog should cancel.
    return OrchestratorLiveness(False, False, False, None, "")


# ---------------------------------------------------------------------------
# Out-of-loop hard watchdog
# ---------------------------------------------------------------------------
# The in-process liveness watchdog (``lifecycle._monitor_background_tasks``) and
# the ``/health`` probe both run *on the event loop*. When the loop is FULLY
# blocked — a synchronous CPU stall, or every pool connection checked out so even
# ``asyncio.timeout``'s own callback can't be scheduled (exactly the failure mode
# ``_bounded_leader_step`` documents it cannot interrupt) — neither can run: the
# in-loop watchdog can't cancel the hung task and ``/health`` can't answer. The
# only remaining backstop is Cloud Run failing enough liveness probes to recycle
# the container, which takes minutes (observed ~6-min wedge windows on prod
# 2026-06-15, during which agent heartbeats 504 and risk reaping).
#
# This OS-thread watchdog closes that gap. A real thread keeps getting scheduled
# even while the event loop is blocked (CPython releases the GIL on blocking
# C-calls and periodically during pure-Python execution), so it can observe a
# stale progress heartbeat and hard-exit the process itself — turning a multi-
# minute Cloud-Run-probe wedge into a bounded one, and dumping every thread's
# stack on the way out so the blocking frame is captured for the source fix.
#
# It is a LAST RESORT: it fires only past ``ORCHESTRATOR_HARD_WATCHDOG_SECONDS``,
# kept strictly greater than ``ORCHESTRATOR_HANG_SECONDS`` so the graceful in-loop
# cancel-and-restart path always gets first chance on a merely-slow (still-
# scheduling) cycle. It is gated on an ``is_guarded`` callable (leadership): a
# follower never pumps the worker heartbeat, so its intentionally-idle marker
# must never be read as a wedge.

ORCHESTRATOR_HARD_WATCHDOG_SECONDS = max(
    int(os.getenv("ORCHESTRATOR_HARD_WATCHDOG_SECONDS", "240")),
    # Floor strictly above the in-loop watchdog's window so the graceful path
    # (detect-stalled→cancel-task→respawn, which completes by ~HANG + a monitor
    # poll) always resolves a recoverable hang before this hard-kills the process.
    ORCHESTRATOR_HANG_SECONDS + 30,
)
# How often the watchdog thread re-checks the heartbeat. Small relative to the
# hard bound so the kill latency past the bound is at most one poll interval.
ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS = float(
    os.getenv("ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS", "15")
)

_hard_watchdog_thread: "threading.Thread | None" = None
_hard_watchdog_stop: "threading.Event | None" = None


def hard_watchdog_should_abort(
    now: datetime | None = None, *, threshold: float | None = None
) -> Tuple[bool, str]:
    """Pure decision for the out-of-loop hard watchdog: ``(abort, reason)``.

    Reuses :func:`evaluate_orchestrator_liveness` with the *hard* threshold so the
    wedge signal/threshold logic stays single-sourced (the cold-start /
    first-cycle-never-progressed cases are handled there too). ``abort`` is True
    only when a heartbeat or cycle WAS recorded and has since gone stale past the
    bound — never for a fresh process or a follower that has recorded nothing.
    """
    if threshold is None:
        threshold = ORCHESTRATOR_HARD_WATCHDOG_SECONDS
    liveness = evaluate_orchestrator_liveness(now, threshold=threshold)
    return (liveness.stalled, liveness.reason)


def _default_hard_abort(reason: str) -> None:
    """Dump every thread's stack (so the blocking frame lands in Cloud Logging for
    the follow-up source fix) then hard-exit so Cloud Run restarts this instance.

    ``os._exit`` is deliberate: a graceful ``sys.exit`` / atexit / async teardown
    would itself run on (or schedule onto) the wedged loop and could block forever.
    """
    try:
        logger.critical(
            "[HARD-WATCHDOG] Orchestrator event loop wedged: %s. The in-loop "
            "watchdog and /health cannot act on a fully-blocked loop; dumping "
            "thread stacks and hard-exiting (os._exit) so Cloud Run restarts "
            "this instance.",
            reason,
        )
    except Exception:
        pass
    try:
        faulthandler.dump_traceback(all_threads=True)
    except Exception:
        pass
    for _stream in (sys.stderr, sys.stdout):
        try:
            _stream.flush()
        except Exception:
            pass
    os._exit(1)


def _hard_watchdog_step(is_guarded, should_abort, abort) -> bool:
    """One watchdog poll. Returns True iff it triggered the abort (the loop then
    stops). Swallows its own errors — a watchdog must never crash its own thread
    and leave the loop unguarded."""
    try:
        if not is_guarded():
            return False
        abort_now, reason = should_abort()
    except Exception:  # pragma: no cover - defensive; never let the guard die
        return False
    if abort_now:
        abort(reason)
        return True
    return False


def _hard_watchdog_loop(
    *, is_guarded, should_abort, abort, stop_event, poll_interval
) -> None:
    # ``stop_event.wait`` returns True only when the event is set (clean
    # shutdown) → exit the loop; otherwise it returns False after ``poll_interval``
    # and we re-check. Using the event as the sleep makes shutdown immediate.
    while not stop_event.wait(poll_interval):
        if _hard_watchdog_step(is_guarded, should_abort, abort):
            return


def start_orchestrator_hard_watchdog(
    *,
    is_guarded,
    poll_interval: float | None = None,
    abort=None,
) -> "threading.Thread":
    """Start (idempotently) the daemon-thread hard watchdog.

    ``is_guarded`` is a no-arg callable returning whether this instance should be
    guarded right now (i.e. it is the background leader and expected to be pumping
    the worker heartbeat). ``abort``/``poll_interval`` are injectable for tests.
    """
    global _hard_watchdog_thread, _hard_watchdog_stop
    if _hard_watchdog_thread is not None and _hard_watchdog_thread.is_alive():
        return _hard_watchdog_thread
    if poll_interval is None:
        poll_interval = ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS
    _hard_watchdog_stop = threading.Event()
    thread = threading.Thread(
        target=_hard_watchdog_loop,
        kwargs=dict(
            is_guarded=is_guarded,
            should_abort=hard_watchdog_should_abort,
            abort=abort or _default_hard_abort,
            stop_event=_hard_watchdog_stop,
            poll_interval=poll_interval,
        ),
        name="orch-hard-watchdog",
        daemon=True,
    )
    _hard_watchdog_thread = thread
    thread.start()
    logger.info(
        "[HARD-WATCHDOG] started — hard_bound=%ss poll=%ss (last-resort process "
        "kill for a fully-blocked leader event loop)",
        ORCHESTRATOR_HARD_WATCHDOG_SECONDS,
        poll_interval,
    )
    return thread


def stop_orchestrator_hard_watchdog() -> None:
    """Signal the watchdog thread to exit. Used by graceful shutdown / tests."""
    global _hard_watchdog_thread, _hard_watchdog_stop
    if _hard_watchdog_stop is not None:
        _hard_watchdog_stop.set()
    _hard_watchdog_thread = None
