/**
 * Forensic instrumentation and diagnostics for debugging agent failures.
 *
 * Traces event-loop lag, heartbeat timing, and final agent state to diagnose
 * silent deaths and process hangs. Writes are synchronous (fs.appendFileSync)
 * to survive subsequent crashes.
 *
 * Files written:
 *   /tmp/eventloop-lag.log    — appended every time setInterval drift > 5s
 *   /tmp/heartbeat-trace.log  — start/end/duration of every heartbeat attempt
 *   /tmp/agent-final-state.log — last-words on beforeExit/exit/SIGTERM
 */

import fs from 'fs';

const EVENTLOOP_LAG_LOG = '/tmp/eventloop-lag.log';
const HEARTBEAT_TRACE_LOG = '/tmp/heartbeat-trace.log';
const AGENT_FINAL_STATE_LOG = '/tmp/agent-final-state.log';

const EVENTLOOP_CHECK_INTERVAL_MS = 2_000;
// Lag > 5s means setInterval drifted by >2.5x its scheduled interval — strong
// signal of event-loop starvation. Under healthy load, drift stays <100ms.
const EVENTLOOP_LAG_THRESHOLD_MS = 5_000;

let eventLoopMonitorTimer: NodeJS.Timeout | null = null;

function appendSyncBestEffort(path: string, line: string) {
  try {
    fs.appendFileSync(path, line);
  } catch {
    // Diagnostic logging must never throw — we'd lose the original failure
    // info we're trying to capture.
  }
}

export function startEventLoopLagMonitor(): void {
  if (eventLoopMonitorTimer) return;
  let lastTickAt = Date.now();
  eventLoopMonitorTimer = setInterval(() => {
    const now = Date.now();
    const drift = now - lastTickAt - EVENTLOOP_CHECK_INTERVAL_MS;
    lastTickAt = now;
    if (drift > EVENTLOOP_LAG_THRESHOLD_MS) {
      const line = `${new Date(now).toISOString()} drift_ms=${drift} (interval=${EVENTLOOP_CHECK_INTERVAL_MS}, threshold=${EVENTLOOP_LAG_THRESHOLD_MS})\n`;
      appendSyncBestEffort(EVENTLOOP_LAG_LOG, line);
    }
  }, EVENTLOOP_CHECK_INTERVAL_MS);
  // Don't keep the process alive just for the monitor.
  if (typeof eventLoopMonitorTimer.unref === 'function') eventLoopMonitorTimer.unref();
}

export function traceHeartbeat(
  attemptKind: 'initial' | 'periodic' | 'retry-409' | 'retry-auth' | 'sync',
  startMs: number,
  endMs: number,
  status: 'ok' | 'err' | 'skip-in-flight' | 'reclaim-wedged',
  detail?: string,
): void {
  const dur = endMs - startMs;
  const line = `${new Date(startMs).toISOString()} kind=${attemptKind} dur_ms=${dur} status=${status}${detail ? ' detail=' + detail.replace(/\n/g, ' ').slice(0, 200) : ''}\n`;
  appendSyncBestEffort(HEARTBEAT_TRACE_LOG, line);
}

export function recordFinalState(reason: string, detail?: string): void {
  const line = `${new Date().toISOString()} reason=${reason}${detail ? ' detail=' + detail.replace(/\n/g, ' ').slice(0, 500) : ''}\n`;
  appendSyncBestEffort(AGENT_FINAL_STATE_LOG, line);
}

let exitMarkersInstalled = false;

export function installExitMarkers(): void {
  // Idempotent: re-entering main() (warm-VM continuity) must not stack
  // duplicate listeners that fire N times on a single exit.
  if (exitMarkersInstalled) return;
  exitMarkersInstalled = true;
  process.on('beforeExit', (code) => {
    recordFinalState('beforeExit', `code=${code}`);
  });
  process.on('exit', (code) => {
    recordFinalState('exit', `code=${code}`);
  });
  // SIGTERM/uncaughtException already have handlers in index.ts that call
  // _crashReport, which calls recordFinalState explicitly. We don't re-register
  // them here — keeping the wiring there preserves the single canonical
  // death path. (This means installExitMarkers only handles the soft-exit
  // paths: beforeExit/exit. Crash paths self-record.)
}
