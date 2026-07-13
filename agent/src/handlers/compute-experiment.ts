/**
 * Compute experiment handler — budgeted compute job provisioning and monitoring.
 *
 * Provisions a custom compute job (VM, container, or specialized hardware) with
 * a specified budget and success criteria, monitors it to a terminal state
 * (SUCCESS, FAILURE, TIMEOUT), and reports results. Compute jobs are durable
 * resources that persist beyond a single task's lifetime; the handler polls
 * backend ComputeJob endpoints until terminal state.
 *
 * Payload specifies compute_type (e.g., "vm", "gpu_job"), resource_config,
 * setup_steps, run_command, and success_criteria (e.g., metric thresholds,
 * exit codes). Used for data-intensive research, training runs, large-scale
 * benchmarks, and other work that exceeds a single Claude Code task budget.
 *
 * Completes with COMPLETED/FAILED based on the compute job's terminal outcome.
 */

import type WebSocket from 'ws';
import {
  strVal, sanitize, sendLog, persistAndPostStatus, apiPost,
} from '../index.js';
import { setPromptSizeTelemetry } from '../telemetry.js';

// ── Handler: Compute Experiment ──────────────────────────────────────────────
//
// NOTE: this handler is launch-and-park ONLY. The agent does not analyse the
// job's result on the VM — the bespoke VM-side analysis leg (a crippled,
// single-turn, no-tools API call over a truncated log that could only write a
// KnowledgeDocument) was removed. Result interpretation is now either the
// backend's deterministic faithful capture or the opt-in judgment agent; see
// backend task_completion._handle_compute_experiment_completion.
//
// Reference compute-heavy task type that runs on the budgeted compute-job
// substrate instead of pinning the agent VM. The agent does NOT clone the repo,
// run Claude, or hold the heavy machine — and it no longer poll-pins its own
// fleet slot for the job's entire duration. It only
//   1. launches a budgeted, auto-torn-down job via POST /api/agent/compute-jobs, and
//   2. PARKS the task on a durable `compute_job_terminal` wake registration keyed
//      to the launched job, releasing its VM/JWT/fleet slot immediately.
//
// The backend monitor accrues cost, enforces the spend cap + wall-clock deadline,
// tears the job VM down, and — when the job reaches a terminal state — fires the
// wake. The leader-owned wake-delivery worker then finalizes THIS task through the
// compute_experiment completion path with the job's honest terminal outcome, final
// cost, and result reference. Killed-by-budget and killed-by-deadline are still
// INTENTIONAL terminal outcomes (the substrate did its job); the backend surfaces
// them as COMPLETED, not crashes.
//
// There are NO direct cloud-provider imports here — every cloud side-effect is
// mediated by the backend launch endpoint and monitor. There is also no agent-side
// poll loop and therefore no `compute_monitor_failed` failure mode: a job that
// never reaches terminal is aged out by the backend reaper at the park deadline,
// not by a stuck agent. Immediate launch-validation failures still fail loudly.

// Wake-source kind for the task park-and-wake primitive. MUST match the backend
// (app.compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND): the monitor fires this kind
// with wake_source_ref=<job id> when the job reaches a terminal state.
const COMPUTE_JOB_TERMINAL_WAKE_KIND = 'compute_job_terminal';

// Slack added on top of the job's own deadline before the BACKEND reaper ages a
// parked task out (park_wake_timeout). The monitor terminates the job at its
// wall-clock deadline and fires the wake well before this, so the deadline is only
// a backstop against a wedged monitor — generous so a healthy run never trips it.
const COMPUTE_PARK_GRACE_SECONDS = 600;

function numOrNull(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

type JobResponse = Record<string, unknown>;

function unwrapJobData(res: unknown): JobResponse | null {
  if (res && typeof res === 'object' && 'data' in res) {
    const data = (res as Record<string, unknown>)['data'];
    if (data && typeof data === 'object') return data as JobResponse;
  }
  return null;
}

function parseJsonFromErrorMessage(message: string): Record<string, unknown> | null {
  const start = message.indexOf('{');
  if (start < 0) return null;
  try {
    const parsed: unknown = JSON.parse(message.slice(start));
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }
  return null;
}

function extractLiveProofPreconditionFailure(error: unknown): Record<string, unknown> | null {
  const message = error instanceof Error ? error.message : String(error);
  if (!message.includes('failed (412)')) return null;
  const parsed = parseJsonFromErrorMessage(message);
  const detail = parsed?.detail
    ?? (parsed?.error && typeof parsed.error === 'object'
      ? (parsed.error as Record<string, unknown>).message
      : null);
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null;
  const detailObj = detail as Record<string, unknown>;
  if (detailObj.error !== 'live_release_precondition_failed') return null;
  return detailObj;
}

function liveProofBlockerLog(detail: Record<string, unknown>): string {
  const message = strVal(detail.message) || 'live-release precondition not satisfied';
  const reason = strVal(detail.reason);
  const evidence = detail.live_release_evidence && typeof detail.live_release_evidence === 'object'
    ? detail.live_release_evidence as Record<string, unknown>
    : null;
  const versionValue = evidence?.release_version_number;
  const version = typeof versionValue === 'number' || typeof versionValue === 'string'
    ? String(versionValue)
    : '';
  const aggregate = evidence ? strVal(evidence.aggregate_serving_status) : '';
  const parts = [`BLOCKER: ${message}`];
  if (reason) parts.push(`reason=${reason}`);
  if (version) parts.push(`release_version=${version}`);
  if (aggregate) parts.push(`aggregate=${aggregate}`);
  return parts.join(' ');
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
  // Forward the optional single-GPU accelerator only when present, so CPU jobs
  // (no accelerator) keep their exact existing launch body.
  const accelerator = strVal(payload.accelerator);
  if (accelerator) body.accelerator = accelerator;
  if (payload.live_proof !== undefined) body.live_proof = payload.live_proof;
  const liveProofMarker = strVal(payload.live_proof_marker);
  if (liveProofMarker) body.live_proof_marker = liveProofMarker;
  if (payload.live_release_proof !== undefined) body.live_proof = payload.live_release_proof;
  const liveReleaseProofMarker = strVal(payload.live_release_proof_marker);
  if (liveReleaseProofMarker) body.live_proof_marker = liveReleaseProofMarker;
  if (payload.requires_live_release !== undefined) body.live_proof = payload.requires_live_release;
  const setupStep = strVal(payload.setup_step);
  if (setupStep) body.setup_step = setupStep;
  // Forward the optional no-progress watchdog window (peer of deadline_seconds) so a
  // stalled job is cut early. Only forwarded when a positive number is present —
  // jobs without it keep their exact existing launch body (inert watchdog).
  const noProgressTimeout = payload.no_progress_timeout_seconds;
  if (typeof noProgressTimeout === 'number' && noProgressTimeout > 0) {
    body.no_progress_timeout_seconds = noProgressTimeout;
  }
  // Forward declared_metric so the platform-executed pre-flight smoke knows
  // which metric to verify is captured. Only forwarded when present — CPU/GPU
  // jobs without a declared metric are byte-for-byte unchanged.
  const declaredMetric = strVal(payload.declared_metric);
  if (declaredMetric) body.declared_metric = declaredMetric;
  // Forward optional container image and registry auth when present.
  const containerImage = strVal(payload.container_image);
  if (containerImage) body.container_image = containerImage;
  if (payload.container_registry_auth && typeof payload.container_registry_auth === 'object' && !Array.isArray(payload.container_registry_auth)) {
    body.container_registry_auth = payload.container_registry_auth;
  }
  return body;
}

export async function handleComputeExperiment(
  taskId: string, payload: Record<string, unknown>, _task: Record<string, unknown>,
  _ghToken: string, _owner: string, _repo: string, ws: WebSocket | null, statusRef: { value: string },
  _model?: string, _effort?: string,
) {
  // No Claude invocation in this handler — record a zero-byte prompt so
  // the task type reports a non-null prompt_size_bytes in its api_call_summary.
  setPromptSizeTelemetry(0, 0, []);

  const command = strVal(payload.command);
  const spendCap = numOrNull(payload.spend_cap_usd);
  const deadlineSeconds = numOrNull(payload.deadline_seconds);
  const experiment = strVal(payload.experiment) || null;

  // Validate launch requirements up front and FAIL loudly on misconfiguration —
  // never silently default a spend cap (would risk unbounded spend). These
  // immediate launch-validation failures are preserved exactly as before; only the
  // post-launch poll/monitor path changed to park-and-wake.
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
    const precondition = extractLiveProofPreconditionFailure(e);
    if (precondition) {
      const logLine = liveProofBlockerLog(precondition);
      sendLog(ws, `[BLOCKED] ${logLine}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        failure_category: 'BLOCKED_PRECONDITION',
        logs: sanitize(JSON.stringify({ error: logLine, detail: precondition, experiment })),
      });
      return;
    }
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

  // ── 2. Park on the compute-job-terminal wake source ──────────────────────────
  // Release this VM/fleet slot now instead of polling the job to terminal. The
  // park registration is FINALIZE-action and keyed to the job id; the backend
  // wake worker runs the compute_experiment completion path when the monitor fires
  // the wake. The park deadline outlasts the job's full worst-case window
  // (declared deadline + any provisioning allowance for GPU jobs) plus grace, so
  // the reaper only ages the task out if the monitor never reports terminal. A
  // duplicate launch returns the same job (backend idempotency), so re-parking
  // targets the same job id and the backend's park guard prevents duplicate
  // registrations.
  //
  // Prefer the backend-computed park_deadline_seconds from polling_metadata (which
  // accounts for the GPU provisioning allowance) over the local fallback so GPU
  // jobs never have their park expire during the substrate's legal provisioning
  // window.
  const pollingMeta = job && typeof job.polling_metadata === 'object' && job.polling_metadata !== null
    ? job.polling_metadata as Record<string, unknown>
    : null;
  const parkDeadlineSeconds = numOrNull(pollingMeta?.park_deadline_seconds)
    ?? (deadlineSeconds as number) + COMPUTE_PARK_GRACE_SECONDS;
  statusRef.value = `Parked on compute job ${jobId} until terminal`;
  sendLog(ws, `Parking task on compute job ${jobId} (releasing VM/slot); backend will finalize on terminal.\n`);
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'PARKED',
    park_request: {
      wake_source_kind: COMPUTE_JOB_TERMINAL_WAKE_KIND,
      wake_source_ref: jobId,
      wake_action: 'finalize',
      deadline_seconds: parkDeadlineSeconds,
      reason: `Parked on compute job ${jobId} terminal`,
      source_metadata: { job_id: jobId, experiment },
    },
  });
}
