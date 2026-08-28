import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock the main agent runner so the handler's API + status helpers are stubbed,
// while keeping the real strVal/sanitize implementations (we only override the
// network/status surface).
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    sendLog: vi.fn(),
    apiPost: vi.fn(),
    apiGet: vi.fn(),
    persistAndPostStatus: vi.fn(),
    runClaudeCode: vi.fn(),
  };
});

// NOTE: import the handler module BEFORE '../index.js' so vitest binds its
// '../index.js' import to the mocked module. If '../index.js' is imported first,
// the mock factory's importOriginal() eagerly loads the real index → handler
// graph and caches a real-bound copy of this handler.
import { handleComputeExperiment } from './compute-experiment.js';
import { apiPost, apiGet, persistAndPostStatus } from '../index.js';
import { ALL_AGENT_TASK_TYPES } from '../taskCapabilities.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

const TASK = { title: 'Compute experiment', description: '', context: {} };

function jobResponse(fields: Record<string, unknown>): { data: Record<string, unknown> } {
  return {
    data: {
      id: 'job-123',
      project_id: 'proj-1',
      task_id: 'task-1',
      backend: 'gcp',
      machine_type: 'e2-standard-8',
      status: 'QUEUED',
      accrued_cost_usd: 0.0,
      spend_cap_usd: 2.5,
      ...fields,
    },
  };
}

const VALID_PAYLOAD = {
  type: 'compute_experiment',
  command: 'python bench.py',
  spend_cap_usd: 2.5,
  deadline_seconds: 600,
  machine_type: 'e2-standard-8',
  experiment: 'perf-sweep-v1',
};

function lastCompletionPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  return calls[calls.length - 1][1];
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── Registry wiring ──────────────────────────────────────────────────────────
// The handler-map registration (handlers.compute_experiment === a function) is
// covered exhaustively in ./index.test.ts. Here we assert the capability is in
// the shared task-type registry and the handler export is callable. (We avoid
// importing the handler registry here: the '../index.js' mock factory's
// importOriginal() loads a real-bound copy of the handler graph, which would
// shadow the mocked-bound handler under test.)
describe('compute_experiment registration', () => {
  it('is a known agent task type', () => {
    expect(ALL_AGENT_TASK_TYPES).toContain('compute_experiment');
  });

  it('exports a callable handler', () => {
    expect(typeof handleComputeExperiment).toBe('function');
  });
});

// ── handleComputeExperiment: launch → park ───────────────────────────────────
// The agent no longer polls the job to terminal or builds the COMPUTE_RESULT
// artifact — it launches the job and PARKS the task on a compute_job_terminal
// wake. The backend monitor + wake worker finalize the task. So there is no
// agent-side poll loop, no classifyOutcome, and no compute_monitor_failed mode.
describe('handleComputeExperiment', () => {
  it('launches the job, then PARKS on the compute-job-terminal wake (no polling)', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'QUEUED' }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    // Launch forwarded the required + optional fields.
    expectCalledWith(apiPost, '/api/agent/compute-jobs', expect.objectContaining({
      command: 'python bench.py',
      spend_cap_usd: 2.5,
      deadline_seconds: 600,
      machine_type: 'e2-standard-8',
    }));

    // The agent must NOT poll the job — that is the backend monitor's job now.
    expect(vi.mocked(apiGet)).not.toHaveBeenCalled();

    const park = lastCompletionPayload();
    expect(park.status).toBe('PARKED');
    const req = park.park_request as Record<string, unknown>;
    expect(req.wake_source_kind).toBe('compute_job_terminal');
    expect(req.wake_source_ref).toBe('job-123');
    expect(req.wake_action).toBe('finalize');
    // Park deadline outlasts the job's own deadline (600s) by the grace window.
    expect(req.deadline_seconds).toBe(600 + 600);
    expect((req.source_metadata as Record<string, unknown>).job_id).toBe('job-123');
    expect((req.source_metadata as Record<string, unknown>).experiment).toBe('perf-sweep-v1');
  });

  it('forwards the live-proof launch marker', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'QUEUED' }));

    await handleComputeExperiment(
      'task-1',
      { ...VALID_PAYLOAD, live_proof_marker: 'v8-live-proof' },
      TASK,
      '',
      'o',
      'r',
      null,
      { value: '' },
    );

    expectCalledWith(apiPost, '/api/agent/compute-jobs', expect.objectContaining({
      live_proof_marker: 'v8-live-proof',
    }));
  });

  it('forwards the complete single-command resume contract', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'QUEUED' }));
    const predecessor = '11111111-1111-4111-8111-111111111111';

    await handleComputeExperiment(
      'task-1',
      {
        ...VALID_PAYLOAD,
        input_artifact_job_id: predecessor,
        declared_metric: 'eval/accuracy',
        no_progress_timeout_seconds: 180,
      },
      TASK,
      '',
      'o',
      'r',
      null,
      { value: '' },
    );

    expectCalledWith(apiPost, '/api/agent/compute-jobs', expect.objectContaining({
      command: 'python bench.py',
      input_artifact_job_id: predecessor,
      declared_metric: 'eval/accuracy',
      no_progress_timeout_seconds: 180,
    }));
  });

  it('fails fast WITHOUT launching when the spend cap is missing', async () => {
    const payload = { type: 'compute_experiment', command: 'python bench.py', deadline_seconds: 600 };
    await handleComputeExperiment('task-1', payload, TASK, '', 'o', 'r', null, { value: '' });

    expect(vi.mocked(apiPost)).not.toHaveBeenCalled();
    const completion = lastCompletionPayload();
    expect(completion.status).toBe('FAILED');
    expect(completion.failure_category).toBe('compute_launch_failed');
    expect(completion.logs as string).toContain('spend_cap_usd');
  });

  it('fails fast WITHOUT launching when the command is missing', async () => {
    const payload = { type: 'compute_experiment', spend_cap_usd: 1, deadline_seconds: 600 };
    await handleComputeExperiment('task-1', payload, TASK, '', 'o', 'r', null, { value: '' });

    expect(vi.mocked(apiPost)).not.toHaveBeenCalled();
    expect(lastCompletionPayload().status).toBe('FAILED');
  });

  it('fails the task (NOT parked) when the launch request throws', async () => {
    vi.mocked(apiPost).mockRejectedValue(new Error('422 unsupported_machine_type'));
    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    expect(vi.mocked(apiGet)).not.toHaveBeenCalled();
    const completion = lastCompletionPayload();
    expect(completion.status).toBe('FAILED');
    expect(completion.failure_category).toBe('compute_launch_failed');
    expect(completion.status).not.toBe('PARKED');
  });

  it('blocks the task when the backend rejects a live-proof precondition', async () => {
    vi.mocked(apiPost).mockRejectedValue(new Error(
      'POST /api/agent/compute-jobs failed (412): {"error":{"code":412,"message":{"error":"live_release_precondition_failed","reason":"release_not_serving_confirmed","message":"waiting on required artifacts: agent_image","live_release_evidence":{"release_version_number":3,"aggregate_serving_status":"unconfirmed"}}}}',
    ));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD, live_proof_marker: 'v8-live-proof' }, TASK, '', 'o', 'r', null, { value: '' });

    expect(vi.mocked(apiGet)).not.toHaveBeenCalled();
    const completion = lastCompletionPayload();
    expect(completion.status).toBe('FAILED');
    expect(completion.failure_category).toBe('BLOCKED_PRECONDITION');
    expect(completion.logs as string).toContain('BLOCKER: waiting on required artifacts: agent_image');
    expect(completion.logs as string).not.toContain('compute_launch_failed');
  });

  it('fails the task (NOT parked) when the launch returns no job id', async () => {
    vi.mocked(apiPost).mockResolvedValue({ data: { project_id: 'proj-1' } });
    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const completion = lastCompletionPayload();
    expect(completion.status).toBe('FAILED');
    expect(completion.failure_category).toBe('compute_launch_failed');
  });

  it('uses backend-supplied park_deadline_seconds from polling_metadata when present', async () => {
    // Simulate a GPU job response where the backend computed a larger park deadline
    // (deadline + provisioning allowance + grace).
    const gpuParkDeadline = 600 + 900 + 600; // 2100s for a T4 job
    vi.mocked(apiPost).mockResolvedValue(jobResponse({
      status: 'QUEUED',
      polling_metadata: { park_deadline_seconds: gpuParkDeadline },
    }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const park = lastCompletionPayload();
    expect(park.status).toBe('PARKED');
    const req = park.park_request as Record<string, unknown>;
    // Must use the backend-supplied value, not deadline + local grace (1200).
    expect(req.deadline_seconds).toBe(gpuParkDeadline);
  });

  it('falls back to deadline + local grace when polling_metadata.park_deadline_seconds is absent', async () => {
    // No polling_metadata in the response — should fall back to the old calculation.
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'QUEUED' }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const park = lastCompletionPayload();
    expect(park.status).toBe('PARKED');
    const req = park.park_request as Record<string, unknown>;
    expect(req.deadline_seconds).toBe(600 + 600); // fallback: deadline + COMPUTE_PARK_GRACE_SECONDS
  });

  it('does not expose raw polling_metadata fields in park source_metadata', async () => {
    // The agent-facing endpoint returns full polling_metadata (agents own the job and
    // need the full metadata). Verify the handler does NOT forward raw polling_metadata
    // fields — including sensitive ones like env, command_log, and live_output — into
    // the park source_metadata that gets stored on the task record.
    vi.mocked(apiPost).mockResolvedValue(jobResponse({
      status: 'QUEUED',
      polling_metadata: {
        park_deadline_seconds: 1200,
        env: { SECRET_API_KEY: 'super-secret-value' },
        command: 'bash run.sh --key=super-secret',
        command_log: { text: 'Authorization: Bearer ghp_SECRETTOKEN\n', truncated: false },
        live_output: { text: 'partial output ghp_SECRETTOKEN\n', truncated: true },
      },
    }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const park = lastCompletionPayload();
    expect(park.status).toBe('PARKED');
    const req = park.park_request as Record<string, unknown>;
    const srcMetaStr = JSON.stringify(req.source_metadata);

    // source_metadata must only carry {job_id, experiment} — raw polling_metadata
    // fields must never flow into the stored task record.
    expect(srcMetaStr).not.toContain('super-secret-value');
    expect(srcMetaStr).not.toContain('ghp_SECRETTOKEN');
    expect(srcMetaStr).not.toContain('command_log');
    expect(srcMetaStr).not.toContain('live_output');
    expect(srcMetaStr).not.toContain('SECRET_API_KEY');

    // Park deadline still uses the backend-supplied value.
    expect(req.deadline_seconds).toBe(1200);
    expect(req.wake_source_ref).toBe('job-123');
  });
});
