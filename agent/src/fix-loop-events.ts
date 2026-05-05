// ── Per-task fix-loop telemetry (agent emitter) ──────────────────────────────
//
// Companion to backend/app/fix_loop_telemetry.py. The agent runs three kinds
// of fix loops (lint, test, and Claude-driven `./check`); this module owns
// the agent-side buffer + synthesizer that lets the backend's
// `Task.fix_loop_history` JSONB column reflect what actually happened.
//
// Flow:
//   1. Wrapper-driven loops (lintFixLoop, testFixLoop in index.ts) call
//      `pushFixLoopEvent` once per iteration.
//   2. Claude-driven `./check` invocations are inferred by walking the
//      tool-trace ring buffer (`synthesizeCheckFixLoopEvents`).
//   3. The heartbeat path drains the buffer and rides the events on the
//      existing `/api/agent/heartbeat` POST. The terminal status post
//      (`persistAndPostStatus`) does a final drain so trailing events
//      land before the task closes.
//
// All shape decisions match `FixLoopEventIn` on the backend — keep field
// names stable across versions; any drift becomes a Pydantic 422 on the
// heartbeat endpoint.
import { createHash } from 'crypto';

export type FixLoopName = 'lint' | 'test' | 'check';

export type FixLoopExitReason =
  | 'passed'
  | 'early_unchanged'
  | 'max_attempts'
  | 'divergence_detected'
  | 'divergence_oscillation'
  | 'divergence_similar_error'
  | 'divergence_diff_revert'
  | 'runaway_detected'
  | 'in_progress';

export type FixLoopEventSource = 'agent_emitted' | 'trace_synthesized';

export interface FixLoopEvent {
  loop: FixLoopName;
  iteration: number;
  fingerprint_hash: string;
  error_tail: string;
  change_summary: string;
  exit_reason: FixLoopExitReason | null;
  source: FixLoopEventSource;
  started_at: string;
  ended_at: string;
}

// In-memory buffer drained on every heartbeat. Capped so a malfunctioning
// loop can't pin unbounded memory between heartbeats; the backend cap is
// 20 events per request, so we mirror that.
const MAX_BUFFERED_EVENTS = 20;
const _buffer: FixLoopEvent[] = [];

export function pushFixLoopEvent(event: FixLoopEvent): void {
  _buffer.push(event);
  while (_buffer.length > MAX_BUFFERED_EVENTS) {
    _buffer.shift();
  }
}

export function drainFixLoopEventBuffer(): FixLoopEvent[] {
  if (_buffer.length === 0) return [];
  const out = _buffer.slice();
  _buffer.length = 0;
  return out;
}

/**
 * Restore drained-but-undelivered events to the head of the buffer when the
 * heartbeat POST fails. Preserves chronological order and respects
 * MAX_BUFFERED_EVENTS — if events queued in the meantime would push us over
 * the cap, the oldest (re-queued) entries are dropped first, mirroring
 * `pushFixLoopEvent` semantics. At-most-once semantics still hold for the
 * cap'd overflow case, but the common case of a transient POST failure no
 * longer silently loses the entire tick's events.
 */
export function requeueFixLoopEvents(events: FixLoopEvent[]): void {
  if (events.length === 0) return;
  _buffer.unshift(...events);
  while (_buffer.length > MAX_BUFFERED_EVENTS) {
    _buffer.shift();
  }
}

export function _peekFixLoopEventBufferForTesting(): FixLoopEvent[] {
  return _buffer.slice();
}

/**
 * Reset the synthesizer's monotonic counter and timestamp watermarks. Call
 * this on warm-role transitions alongside `clearToolTrace()` — without it,
 * the next role's `./check` events continue numbering from the prior role
 * and inherit a stale `started_at`. Buffer is intentionally left intact so
 * any unflushed events from the prior role still ride out on the next
 * heartbeat (terminal flush handles the same case for cold exits).
 */
export function resetCheckSynthesizerState(): void {
  _checkSynthesizerState.lastSeenTimestamp = null;
  _checkSynthesizerState.iteration = 0;
  _checkSynthesizerState.lastEventEndedAt = null;
  _checkOutcomes.length = 0;
}

export function _resetFixLoopEventStateForTesting(): void {
  _buffer.length = 0;
  resetCheckSynthesizerState();
}

/** SHA-256 of the normalized fingerprint, hex, first 16 chars. */
export function fingerprintHash(fingerprint: string): string {
  return createHash('sha256').update(fingerprint).digest('hex').slice(0, 16);
}

// ── Trace synthesizer (Claude-driven `./check`) ──────────────────────────────
//
// The newer code-change flow runs Claude in a single session and lets it
// invoke `./check` itself. There is no explicit iteration counter, so the
// only signal we have is the tool-trace ring buffer. We walk that buffer
// looking for Bash invocations of `./check` and emit one synthesized
// FixLoopEvent per invocation.
//
// State persists across walks so a re-walk of the (only-20-deep) ring
// buffer doesn't re-emit. Backend de-dups by (loop, iteration) anyway, but
// keeping the iteration counter monotonic on the agent side avoids
// burning the per-heartbeat 20-event cap on duplicates.

// ── Check-outcome ring (runaway watchdog feed) ────────────────────────────────
//
// Records pass/fail for each Claude-driven `./check` call in the session.
// The watchdog in index.ts reads this to detect thrashing loops and fire
// SIGTERM before the wall-clock timeout. `resetCheckSynthesizerState` clears
// it on warm-role transitions so a new role starts with a clean slate.

const _checkOutcomes: boolean[] = [];

export function recordCheckOutcome(passed: boolean): void {
  _checkOutcomes.push(passed);
}

/**
 * Returns { totalChecks, shouldTrigger } where shouldTrigger is true when
 * `totalChecks >= minInvocations` AND the last `consecutiveFailures` checks
 * all failed. Safe to call with zero or few recorded outcomes.
 */
export function getCheckRunawayStatus(
  minInvocations: number,
  consecutiveFailures: number,
): { totalChecks: number; shouldTrigger: boolean } {
  const total = _checkOutcomes.length;
  if (total < consecutiveFailures) return { totalChecks: total, shouldTrigger: false };
  const lastN = _checkOutcomes.slice(-consecutiveFailures);
  return {
    totalChecks: total,
    shouldTrigger: total >= minInvocations && lastN.every(v => !v),
  };
}

export function _peekCheckOutcomesForTesting(): boolean[] {
  return _checkOutcomes.slice();
}

const _checkSynthesizerState = {
  lastSeenTimestamp: null as string | null,
  iteration: 0,
  // ISO timestamp of the most recent synthesized check event — used as the
  // `started_at` for the next event so back-to-back invocations carry a
  // sensible interval rather than collapsing to the synthesis time.
  lastEventEndedAt: null as string | null,
};

export interface ToolTraceEntryLike {
  t: string;
  tool: string;
  input_preview: string;
}

/** Heuristic: does this Bash input_preview invoke the project's check helper? */
export function isCheckInvocation(inputPreview: string): boolean {
  // The Bash tool input is JSON `{"command":"./check ..."}`. The preview is
  // truncated at 200 chars by recordToolUse, so we just substring-match.
  // Require the next char after `./check` to be whitespace OR a JSON string
  // terminator — that excludes `./checker` and `./check-something` while
  // still matching `./check`, `./check test`, and `./check\n`.
  return /\.\/check(?:\s|")/.test(inputPreview);
}

function summarizeToolsBetween(
  trace: ToolTraceEntryLike[],
  startIdx: number,
  endIdx: number,
): string {
  const counts = new Map<string, number>();
  for (let i = startIdx; i < endIdx; i++) {
    const tool = trace[i].tool;
    counts.set(tool, (counts.get(tool) ?? 0) + 1);
  }
  if (counts.size === 0) return '(no tool calls between this and previous ./check)';
  return Array.from(counts.entries())
    .map(([t, n]) => `${t}:${n}`)
    .join(', ');
}

/**
 * Walk the tool-trace ring buffer and synthesize one FixLoopEvent per
 * `./check` invocation that hasn't been seen before. Pure of side effects
 * other than advancing the synthesizer's monotonic state — does NOT push
 * to the buffer (callers do that, so they can flush atomically with the
 * heartbeat).
 */
export function synthesizeCheckFixLoopEvents(
  trace: ToolTraceEntryLike[],
): FixLoopEvent[] {
  if (trace.length === 0) return [];
  const events: FixLoopEvent[] = [];
  // Find indices of ./check invocations that are newer than what we've
  // already synthesized. We cannot use array index alone because the ring
  // buffer shifts entries off the front; timestamps are the only stable
  // ordering across walks.
  for (let i = 0; i < trace.length; i++) {
    const entry = trace[i];
    if (entry.tool !== 'Bash') continue;
    if (!isCheckInvocation(entry.input_preview)) continue;
    if (
      _checkSynthesizerState.lastSeenTimestamp !== null &&
      entry.t <= _checkSynthesizerState.lastSeenTimestamp
    ) {
      continue;
    }
    // Find prior ./check (or buffer start) so we can summarize the tool
    // calls between them. Look only inside the current trace window — the
    // older ./check may already have rolled out of the ring.
    let startIdx = 0;
    for (let j = i - 1; j >= 0; j--) {
      const prev = trace[j];
      if (prev.tool === 'Bash' && isCheckInvocation(prev.input_preview)) {
        startIdx = j + 1;
        break;
      }
    }
    const changeSummary = summarizeToolsBetween(trace, startIdx, i);
    _checkSynthesizerState.iteration += 1;
    const event: FixLoopEvent = {
      loop: 'check',
      iteration: _checkSynthesizerState.iteration,
      // We don't have stderr/stdout for the Bash call here — the trace
      // only carries the input. Hash the input_preview itself so two
      // identical invocations share a fingerprint and the backend can
      // still detect "ran the same thing twice in a row" downstream.
      fingerprint_hash: fingerprintHash(entry.input_preview),
      error_tail: '',
      change_summary: changeSummary,
      exit_reason: 'in_progress',
      source: 'trace_synthesized',
      started_at: _checkSynthesizerState.lastEventEndedAt ?? entry.t,
      ended_at: entry.t,
    };
    events.push(event);
    _checkSynthesizerState.lastSeenTimestamp = entry.t;
    _checkSynthesizerState.lastEventEndedAt = entry.t;
  }
  return events;
}
