import type WebSocket from 'ws';
import {
  strVal, sanitize, sendLog, persistAndPostStatus, apiPost, apiGet,
} from '../index.js';

// ── Handler: Compute Experiment ──────────────────────────────────────────────
//
// Reference compute-heavy task type that runs on the budgeted compute-job
// substrate instead of pinning the agent VM. The agent does NOT clone the repo,
// run Claude, or hold the heavy machine: it only
//   1. launches a budgeted, auto-torn-down job via POST /api/agent/compute-jobs,
//   2. polls GET /api/agent/compute-jobs/{id} until the job reaches a terminal
//      state (the backend monitor accrues cost, enforces the spend cap + wall
//      clock deadline, and tears the job VM down), and
//   3. completes the task with the job's terminal outcome and final cost.
//
// There are NO direct cloud-provider imports here — every cloud side-effect is
// mediated by the backend launch/poll endpoints. Killed-by-budget and
// killed-by-deadline are INTENTIONAL terminal outcomes (the substrate did its
// job), surfaced in the result artifact — they are NOT treated as generic agent
// crashes.

// Terminal compute-job OUTCOMEs (ComputeJobOutcome on the backend). Keyed off
// `outcome` rather than `status`, because a spend-cap kill sets status=FAILED
// while outcome=SPEND_CAP_EXCEEDED — the outcome is the honest signal.
const OUTCOME_SUCCESS = 'SUCCESS';
const OUTCOME_FAILURE = 'FAILURE';
const OUTCOME_CANCELLED = 'CANCELLED';
const OUTCOME_TIMED_OUT = 'TIMED_OUT';
const OUTCOME_SPEND_CAP_EXCEEDED = 'SPEND_CAP_EXCEEDED';

// Terminal compute-job STATUSes (ComputeJobStatus), used as the fall-back signal
// when a terminal job response omits `outcome` (older backend / race).
const TERMINAL_JOB_STATUSES = new Set([
  'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT',
]);

// How often the agent polls the job (the backend monitor advances jobs on a
// ~15s cadence, so polling faster buys nothing).
const DEFAULT_POLL_INTERVAL_MS = 15_000;
// Slack added on top of the job's own deadline before the AGENT gives up
// monitoring. The backend terminates the job at its wall-clock deadline, so the
// agent should comfortably observe that terminal transition first; this is only
// a backstop against a stuck monitor.
const AGENT_POLL_GRACE_MS = 120_000;
const AGENT_POLL_GRACE_POLLS = 5;
// Absolute ceiling on poll iterations so a misconfigured/zero deadline can't
// spin forever.
const MAX_POLL_ITERATIONS = 5_000;
// Consecutive transient poll errors tolerated before the agent gives up
// monitoring (each apiGet already retries internally).
const MAX_CONSECUTIVE_POLL_ERRORS = 5;

// Injectable sleep so tests can drive the multi-poll path without real waits.
let _sleepImpl: (ms: number) => Promise<void> = (ms) =>
  new Promise((resolve) => setTimeout(resolve, ms));

/** Test-only: override (or reset with null) the poll-loop sleep. */
export function _setComputeExperimentSleepForTesting(
  fn: ((ms: number) => Promise<void>) | null,
): void {
  _sleepImpl = fn ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
}

function numOrNull(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

// Reserved top-level result keys. A reported metric whose name collides with one
// of these is skipped so it can never overwrite a job-level field. Must stay in
// sync with the keys written into `result` below.
const RESERVED_RESULT_KEYS = new Set<string>([
  'job_id', 'backend', 'machine_type', 'status', 'outcome', 'intentional_terminal',
  'killed_by', 'final_cost_usd', 'accrued_cost_usd', 'spend_cap_usd', 'terminal_reason',
  'poll_attempts', 'result_reference', 'experiment',
]);

type JobResponse = Record<string, unknown>;

/**
 * Read the command-reported result metrics off the polled terminal job (the
 * backend stamps them into polling_metadata.result_metrics) and flatten each one
 * as a top-level NUMERIC key on `result`. Metrics colliding with a reserved
 * job-level key are skipped; non-numeric values are dropped.
 */
function forwardResultMetrics(result: Record<string, unknown>, job: JobResponse): void {
  const meta = job.polling_metadata;
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return;
  const reported = (meta as Record<string, unknown>).result_metrics;
  if (!reported || typeof reported !== 'object' || Array.isArray(reported)) return;
  for (const [name, value] of Object.entries(reported as Record<string, unknown>)) {
    if (RESERVED_RESULT_KEYS.has(name)) continue;
    const numeric = numOrNull(value);
    if (numeric !== null) result[name] = numeric;
  }
}

function unwrapJobData(res: unknown): JobResponse | null {
  if (res && typeof res === 'object' && 'data' in res) {
    const data = (res as { data: unknown }).data;
    if (data && typeof data === 'object') return data as JobResponse;
  }
  return null;
}

function isTerminalJob(job: JobResponse): boolean {
  const outcome = strVal(job.outcome);
  if (outcome) return true; // outcome is only set once the job is terminal
  return TERMINAL_JOB_STATUSES.has(strVal(job.status));
}

export interface PollOptions {
  pollIntervalMs?: number;
  maxWaitMs?: number;
  maxIterations?: number;
}

export interface PollResult {
  job: JobResponse | null;
  /** True when the loop returned because the job reached a terminal state. */
  terminal: boolean;
  /** Why a non-terminal loop ended ('agent_deadline' | 'poll_errors'). */
  reason?: string;
  polls: number;
}

/**
 * Poll a launched compute job until it reaches a terminal state, the agent's own
 * monitoring deadline elapses, or polling errors out repeatedly. Updates
 * statusRef so the background heartbeat reflects live progress.
 */
export async function pollComputeJobUntilTerminal(
  jobId: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  opts: PollOptions = {},
): Promise<PollResult> {
  const pollIntervalMs = opts.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const maxWaitMs = opts.maxWaitMs ?? Number.POSITIVE_INFINITY;
  const maxIterations = opts.maxIterations ?? MAX_POLL_ITERATIONS;

  const startedAt = Date.now();
  let lastJob: JobResponse | null = null;
  let consecutiveErrors = 0;

  for (let i = 0; i < maxIterations; i++) {
    let job: JobResponse | null = null;
    try {
      const res = await apiGet(`/api/agent/compute-jobs/${jobId}`);
      job = unwrapJobData(res);
      consecutiveErrors = 0;
    } catch (e) {
      consecutiveErrors += 1;
      sendLog(ws, `[WARNING] compute-job poll failed (${consecutiveErrors}/${MAX_CONSECUTIVE_POLL_ERRORS}): ${e instanceof Error ? e.message : String(e)}\n`);
      if (consecutiveErrors >= MAX_CONSECUTIVE_POLL_ERRORS) {
        return { job: lastJob, terminal: false, reason: 'poll_errors', polls: i + 1 };
      }
      await _sleepImpl(pollIntervalMs);
      continue;
    }

    if (job === null) {
      // A 2xx with an unexpected body shape — treat like a transient error.
      consecutiveErrors += 1;
      sendLog(ws, `[WARNING] compute-job poll returned no job data (${consecutiveErrors}/${MAX_CONSECUTIVE_POLL_ERRORS})\n`);
      if (consecutiveErrors >= MAX_CONSECUTIVE_POLL_ERRORS) {
        return { job: lastJob, terminal: false, reason: 'poll_errors', polls: i + 1 };
      }
      await _sleepImpl(pollIntervalMs);
      continue;
    }

    lastJob = job;
    const status = strVal(job.status);
    const cost = numOrNull(job.accrued_cost_usd);
    statusRef.value = `Monitoring compute job ${jobId} (${status || 'unknown'}${cost !== null ? `, $${cost.toFixed(4)}` : ''})...`;

    if (isTerminalJob(job)) {
      return { job, terminal: true, polls: i + 1 };
    }

    if (Date.now() - startedAt >= maxWaitMs) {
      return { job, terminal: false, reason: 'agent_deadline', polls: i + 1 };
    }

    await _sleepImpl(pollIntervalMs);
  }

  return { job: lastJob, terminal: false, reason: 'agent_deadline', polls: maxIterations };
}

interface OutcomeClassification {
  taskStatus: 'COMPLETED' | 'FAILED';
  intentional: boolean;
  killedBy: 'budget' | 'deadline' | null;
}

/**
 * Map a terminal job's outcome to a task disposition. SUCCESS, budget-kill,
 * deadline-kill and cancellation are intentional terminal outcomes → COMPLETED.
 * Only a genuine compute-command FAILURE fails the task.
 */
export function classifyOutcome(outcome: string, status: string): OutcomeClassification {
  const effective = outcome || status;
  switch (effective) {
    case OUTCOME_SPEND_CAP_EXCEEDED:
      return { taskStatus: 'COMPLETED', intentional: true, killedBy: 'budget' };
    case OUTCOME_TIMED_OUT:
    case 'TIMED_OUT':
      return { taskStatus: 'COMPLETED', intentional: true, killedBy: 'deadline' };
    case OUTCOME_SUCCESS:
    case 'SUCCEEDED':
      return { taskStatus: 'COMPLETED', intentional: true, killedBy: null };
    case OUTCOME_CANCELLED:
    case 'CANCELLED':
      return { taskStatus: 'COMPLETED', intentional: true, killedBy: null };
    case OUTCOME_FAILURE:
    case 'FAILED':
      return { taskStatus: 'FAILED', intentional: false, killedBy: null };
    default:
      // Unknown terminal outcome — surface as a failure so it doesn't silently
      // read as a clean success.
      return { taskStatus: 'FAILED', intentional: false, killedBy: null };
  }
}

function buildLaunchBody(payload: Record<string, unknown>): Record<string, unknown> {
  const body: Record<string, unknown> = {
    command: strVal(payload.command),
    spend_cap_usd: numOrNull(payload.spend_cap_usd),
    deadline_seconds: numOrNull(payload.deadline_seconds),
  };
  // Only forward optional launch fields when present, so backend defaults apply.
  const machineType = strVal(payload.machine_type);
  if (machineType) body.machine_type = machineType;
  const cwd = strVal(payload.cwd);
  if (cwd) body.cwd = cwd;
  if (payload.env && typeof payload.env === 'object' && !Array.isArray(payload.env)) {
    body.env = payload.env;
  }
  const backendName = strVal(payload.backend_name);
  if (backendName) body.backend_name = backendName;
  const provisioningModel = strVal(payload.provisioning_model);
  if (provisioningModel) body.provisioning_model = provisioningModel;
  return body;
}

export async function handleComputeExperiment(
  taskId: string, payload: Record<string, unknown>, _task: Record<string, unknown>,
  _ghToken: string, _owner: string, _repo: string, ws: WebSocket | null, statusRef: { value: string },
  _model?: string, _effort?: string,
) {
  const command = strVal(payload.command);
  const spendCap = numOrNull(payload.spend_cap_usd);
  const deadlineSeconds = numOrNull(payload.deadline_seconds);
  const experiment = strVal(payload.experiment) || null;

  // Validate launch requirements up front and FAIL loudly on misconfiguration —
  // never silently default a spend cap (would risk unbounded spend).
  const configErrors: string[] = [];
  if (!command) configErrors.push('command is required');
  if (spendCap === null || spendCap <= 0) configErrors.push('spend_cap_usd must be a positive number');
  if (deadlineSeconds === null || deadlineSeconds <= 0) configErrors.push('deadline_seconds must be a positive number');
  if (configErrors.length > 0) {
    const msg = `Compute experiment misconfigured: ${configErrors.join('; ')}`;
    sendLog(ws, `[ERROR] ${msg}\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'compute_launch_failed',
      logs: sanitize(JSON.stringify({ error: msg, experiment })),
    });
    return;
  }

  // ── 1. Launch ──────────────────────────────────────────────────────────────
  statusRef.value = 'Launching compute job...';
  sendLog(ws, `Launching compute job (cap=$${spendCap}, deadline=${deadlineSeconds}s)...\n`);
  let job: JobResponse | null;
  try {
    const res = await apiPost('/api/agent/compute-jobs', buildLaunchBody(payload));
    job = unwrapJobData(res);
  } catch (e) {
    const msg = `Compute job launch failed: ${e instanceof Error ? e.message : String(e)}`;
    sendLog(ws, `[ERROR] ${msg}\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'compute_launch_failed',
      logs: sanitize(JSON.stringify({ error: msg, experiment })),
    });
    return;
  }

  const jobId = job ? strVal(job.id) : '';
  if (!jobId) {
    const msg = 'Compute job launch returned no job id';
    sendLog(ws, `[ERROR] ${msg}\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'compute_launch_failed',
      logs: sanitize(JSON.stringify({ error: msg, experiment })),
    });
    return;
  }
  sendLog(ws, `Compute job launched: ${jobId}\n`);

  // ── 2. Poll until terminal ───────────────────────────────────────────────────
  // Outlast the job's own deadline so the agent observes the backend-driven
  // terminal transition (cap/deadline/finish) rather than giving up first.
  const maxWaitMs = (deadlineSeconds as number) * 1_000
    + AGENT_POLL_GRACE_MS
    + AGENT_POLL_GRACE_POLLS * DEFAULT_POLL_INTERVAL_MS;
  statusRef.value = `Monitoring compute job ${jobId}...`;
  const pollResult = await pollComputeJobUntilTerminal(jobId, ws, statusRef, { maxWaitMs });
  const finalJob = pollResult.job;

  // ── 3. Complete ──────────────────────────────────────────────────────────────
  if (!pollResult.terminal || finalJob === null) {
    // The agent could not confirm a terminal state (monitor stuck or polling
    // errored out). This IS an agent-side failure — the job may still be
    // running and the backend reaper/orphan sweep will reclaim its VM.
    const msg = `Compute job ${jobId} did not reach a terminal state under agent monitoring (reason=${pollResult.reason ?? 'unknown'}, polls=${pollResult.polls})`;
    sendLog(ws, `[ERROR] ${msg}\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'compute_monitor_failed',
      logs: sanitize(JSON.stringify({
        error: msg,
        job_id: jobId,
        experiment,
        last_status: finalJob ? strVal(finalJob.status) : null,
      })),
    });
    return;
  }

  const outcome = strVal(finalJob.outcome);
  const status = strVal(finalJob.status);
  const classification = classifyOutcome(outcome, status);
  const finalCost = numOrNull(finalJob.final_cost_usd) ?? numOrNull(finalJob.accrued_cost_usd);

  const result: Record<string, unknown> = {
    job_id: jobId,
    backend: strVal(finalJob.backend) || null,
    machine_type: strVal(finalJob.machine_type) || null,
    status: status || null,
    outcome: (outcome || status) || null,
    intentional_terminal: classification.intentional,
    killed_by: classification.killedBy,
    final_cost_usd: finalCost,
    accrued_cost_usd: numOrNull(finalJob.accrued_cost_usd),
    spend_cap_usd: numOrNull(finalJob.spend_cap_usd),
    terminal_reason: strVal(finalJob.terminal_reason) || null,
    poll_attempts: pollResult.polls,
    result_reference: strVal(finalJob.result_reference) || null,
    experiment,
  };

  // Flatten command-reported result metrics to the TOP LEVEL as numeric keys so
  // the downstream gate can read them by name (obj.get(metricName)). A metric
  // whose name collides with a reserved job-level key is SKIPPED — it must never
  // overwrite the job-level field.
  forwardResultMetrics(result, finalJob);

  const killedSuffix = classification.killedBy ? ` (killed by ${classification.killedBy})` : '';
  sendLog(ws, `Compute job ${jobId} terminal: outcome=${(outcome || status) || 'unknown'}${killedSuffix}, final_cost=$${finalCost ?? 'n/a'}\n`);

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: classification.taskStatus,
    artifact_type: 'COMPUTE_RESULT',
    logs: sanitize(JSON.stringify(result)),
    ...(classification.taskStatus === 'FAILED'
      ? { failure_category: 'compute_job_failed' }
      : {}),
  });
}
