import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

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
import { expectCalledWith, expectNotCalledWith } from '../utils/typed-mock-call.js';

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
});

// ── handleComputeExperiment: VM-substrate analysis mode ──────────────────────
// When the backend re-queues a compute_experiment with an analysis request
// bundle on task.context (because it has no ANTHROPIC_API_KEY), the handler calls
// the Anthropic Messages API directly (non-agentic, no subprocess), submits the
// produced analysis to the backend for authoritative grounding, then posts the
// honest completion using the backend-supplied COMPUTE_RESULT logs. It must NOT
// launch a new job.
describe('handleComputeExperiment — VM analysis mode', () => {
  const RESULT_LOGS = JSON.stringify({ job_id: 'job-123', outcome: 'SUCCESS', final_cost_usd: 0.37 });
  const ANALYSIS_TASK = {
    title: 'Compute experiment',
    description: '',
    context: {
      compute_vm_analysis_request: {
        prompt: 'Analyze the durable artifacts and cite gs://bucket/bundle.tar.gz ...',
        result_logs: RESULT_LOGS,
      },
    },
  };
  const ANALYSIS_DOC = {
    title: 'VM analysis',
    summary: 'Grounded summary citing the artifact bundle.',
    body: '# Analysis\n\nThe run cleared the bar.',
    source_citations: ['gs://bucket/bundle.tar.gz'],
  };

  beforeEach(() => {
    process.env.ANTHROPIC_API_KEY = 'test-key';
  });

  afterEach(() => {
    delete process.env.ANTHROPIC_API_KEY;
    vi.unstubAllGlobals();
  });

  it('calls Anthropic API directly, submits the analysis, and completes — without launching a job', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        content: [{
          type: 'text',
          text: 'Here is the analysis:\n```json\n' + JSON.stringify(ANALYSIS_DOC) + '\n```\n',
        }],
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiPost).mockResolvedValue({ status: 'success' });

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, ANALYSIS_TASK, '', 'o', 'r', null, { value: '' });

    // The Anthropic Messages API was called directly (non-agentic, no runClaudeCode subprocess).
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.anthropic.com/v1/messages',
      expect.objectContaining({ method: 'POST' }),
    );
    // No compute job was launched — this is the analysis leg, not the launch leg.
    expectNotCalledWith(apiPost, '/api/agent/compute-jobs', expect.anything());
    // The produced analysis was submitted to the backend grounding endpoint.
    const submitCall = vi.mocked(apiPost).mock.calls.find((c) => c[0] === '/api/agent/compute-analysis');
    if (!submitCall) throw new Error('expected a POST to /api/agent/compute-analysis');
    const submitBody = submitCall[1] as Record<string, unknown>;
    expect(submitBody.job_id).toBe('job-123');
    const submittedAnalysis = submitBody.analysis as Record<string, unknown>;
    expect(submittedAnalysis.summary).toBe('Grounded summary citing the artifact bundle.');
    expect(submittedAnalysis.source_citations).toEqual(['gs://bucket/bundle.tar.gz']);
    // The honest terminal completion carries the backend-authoritative logs.
    const completion = lastCompletionPayload();
    expect(completion.status).toBe('COMPLETED');
    expect(completion.artifact_type).toBe('COMPUTE_RESULT');
    expect(completion.logs as string).toContain('job-123');
  });

  it('still completes (no analysis submitted) when the API call fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: () => Promise.resolve('Internal Server Error'),
    }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, ANALYSIS_TASK, '', 'o', 'r', null, { value: '' });

    // No submission when the model produced nothing usable.
    expectNotCalledWith(apiPost, '/api/agent/compute-analysis', expect.anything());
    // The honest completion still posts so the bounded retry can re-attempt.
    const completion = lastCompletionPayload();
    expect(completion.status).toBe('COMPLETED');
    expect(completion.artifact_type).toBe('COMPUTE_RESULT');
  });
});
