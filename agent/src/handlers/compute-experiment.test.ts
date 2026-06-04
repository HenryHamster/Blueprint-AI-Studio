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
  };
});

// NOTE: import the handler module BEFORE '../index.js' so vitest binds its
// '../index.js' import to the mocked module. If '../index.js' is imported first,
// the mock factory's importOriginal() eagerly loads the real index → handler
// graph and caches a real-bound copy of this handler.
import {
  handleComputeExperiment,
  classifyOutcome,
  pollComputeJobUntilTerminal,
  _setComputeExperimentSleepForTesting,
} from './compute-experiment.js';
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
      status: 'RUNNING',
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
  // No real waiting between polls.
  _setComputeExperimentSleepForTesting(() => Promise.resolve());
});

afterEach(() => {
  _setComputeExperimentSleepForTesting(null);
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

// ── classifyOutcome ──────────────────────────────────────────────────────────
describe('classifyOutcome', () => {
  it('SUCCESS → COMPLETED, intentional, not killed', () => {
    expect(classifyOutcome('SUCCESS', 'SUCCEEDED')).toEqual({
      taskStatus: 'COMPLETED', intentional: true, killedBy: null,
    });
  });

  it('SPEND_CAP_EXCEEDED → COMPLETED, killed by budget (status FAILED is masked by outcome)', () => {
    expect(classifyOutcome('SPEND_CAP_EXCEEDED', 'FAILED')).toEqual({
      taskStatus: 'COMPLETED', intentional: true, killedBy: 'budget',
    });
  });

  it('TIMED_OUT → COMPLETED, killed by deadline', () => {
    expect(classifyOutcome('TIMED_OUT', 'TIMED_OUT')).toEqual({
      taskStatus: 'COMPLETED', intentional: true, killedBy: 'deadline',
    });
  });

  it('CANCELLED → COMPLETED, intentional', () => {
    expect(classifyOutcome('CANCELLED', 'CANCELLED')).toEqual({
      taskStatus: 'COMPLETED', intentional: true, killedBy: null,
    });
  });

  it('FAILURE → FAILED, not intentional', () => {
    expect(classifyOutcome('FAILURE', 'FAILED')).toEqual({
      taskStatus: 'FAILED', intentional: false, killedBy: null,
    });
  });

  it('falls back to status when outcome is empty (SUCCEEDED → COMPLETED)', () => {
    expect(classifyOutcome('', 'SUCCEEDED').taskStatus).toBe('COMPLETED');
  });

  it('unknown terminal outcome → FAILED (never a silent success)', () => {
    expect(classifyOutcome('WEIRD', 'WEIRD').taskStatus).toBe('FAILED');
  });
});

// ── pollComputeJobUntilTerminal ──────────────────────────────────────────────
describe('pollComputeJobUntilTerminal', () => {
  it('polls until the job reaches a terminal state', async () => {
    vi.mocked(apiGet)
      .mockResolvedValueOnce(jobResponse({ status: 'STARTING' }))
      .mockResolvedValueOnce(jobResponse({ status: 'RUNNING', accrued_cost_usd: 0.5 }))
      .mockResolvedValueOnce(jobResponse({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.1 }));

    const statusRef = { value: '' };
    const result = await pollComputeJobUntilTerminal('job-123', null, statusRef, { pollIntervalMs: 1 });

    expect(result.terminal).toBe(true);
    expect(result.polls).toBe(3);
    expect(vi.mocked(apiGet)).toHaveBeenCalledTimes(3);
    expectCalledWith(apiGet, '/api/agent/compute-jobs/job-123');
    expect(result.job?.outcome).toBe('SUCCESS');
  });

  it('stops with reason agent_deadline when the wait budget elapses before terminal', async () => {
    vi.mocked(apiGet).mockResolvedValue(jobResponse({ status: 'RUNNING' }));
    const result = await pollComputeJobUntilTerminal('job-123', null, { value: '' }, {
      pollIntervalMs: 1, maxWaitMs: 0,
    });
    expect(result.terminal).toBe(false);
    expect(result.reason).toBe('agent_deadline');
  });

  it('gives up after repeated poll errors', async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error('network down'));
    const result = await pollComputeJobUntilTerminal('job-123', null, { value: '' }, { pollIntervalMs: 1 });
    expect(result.terminal).toBe(false);
    expect(result.reason).toBe('poll_errors');
  });
});

// ── handleComputeExperiment ──────────────────────────────────────────────────
describe('handleComputeExperiment', () => {
  it('launches the job, forwarding command/cap/deadline, then polls and completes on SUCCESS', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'STARTING' }));
    vi.mocked(apiGet).mockResolvedValue(
      jobResponse({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.23 }),
    );

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    // Launch forwarded the required + optional fields.
    expectCalledWith(apiPost, '/api/agent/compute-jobs', expect.objectContaining({
      command: 'python bench.py',
      spend_cap_usd: 2.5,
      deadline_seconds: 600,
      machine_type: 'e2-standard-8',
    }));

    const completion = lastCompletionPayload();
    expect(completion.status).toBe('COMPLETED');
    expect(completion.artifact_type).toBe('COMPUTE_RESULT');
    const result = JSON.parse(completion.logs as string) as Record<string, unknown>;
    expect(result.outcome).toBe('SUCCESS');
    expect(result.final_cost_usd).toBe(1.23);
    expect(result.killed_by).toBeNull();
    expect(result.intentional_terminal).toBe(true);
    expect(result.experiment).toBe('perf-sweep-v1');
    expect(result.job_id).toBe('job-123');
  });

  it('treats a budget kill as an intentional COMPLETED outcome, not a crash', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'RUNNING' }));
    vi.mocked(apiGet).mockResolvedValue(jobResponse({
      status: 'FAILED', outcome: 'SPEND_CAP_EXCEEDED', final_cost_usd: 2.5,
      terminal_reason: 'compute job terminated at spend cap',
    }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const completion = lastCompletionPayload();
    expect(completion.status).toBe('COMPLETED');
    expect(completion.failure_category).toBeUndefined();
    const result = JSON.parse(completion.logs as string) as Record<string, unknown>;
    expect(result.killed_by).toBe('budget');
    expect(result.intentional_terminal).toBe(true);
    expect(result.final_cost_usd).toBe(2.5);
  });

  it('treats a deadline kill as an intentional COMPLETED outcome', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'RUNNING' }));
    vi.mocked(apiGet).mockResolvedValue(jobResponse({
      status: 'TIMED_OUT', outcome: 'TIMED_OUT', final_cost_usd: 0.9,
    }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const completion = lastCompletionPayload();
    expect(completion.status).toBe('COMPLETED');
    const result = JSON.parse(completion.logs as string) as Record<string, unknown>;
    expect(result.killed_by).toBe('deadline');
  });

  it('fails the task on a genuine compute-command FAILURE', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'RUNNING' }));
    vi.mocked(apiGet).mockResolvedValue(jobResponse({ status: 'FAILED', outcome: 'FAILURE' }));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const completion = lastCompletionPayload();
    expect(completion.status).toBe('FAILED');
    expect(completion.failure_category).toBe('compute_job_failed');
    expect(completion.artifact_type).toBe('COMPUTE_RESULT');
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

  it('fails the task when the launch request throws', async () => {
    vi.mocked(apiPost).mockRejectedValue(new Error('422 unsupported_machine_type'));
    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    expect(vi.mocked(apiGet)).not.toHaveBeenCalled();
    const completion = lastCompletionPayload();
    expect(completion.status).toBe('FAILED');
    expect(completion.failure_category).toBe('compute_launch_failed');
  });

  it('fails with compute_monitor_failed when the job never reaches a terminal state', async () => {
    vi.mocked(apiPost).mockResolvedValue(jobResponse({ status: 'RUNNING' }));
    vi.mocked(apiGet).mockRejectedValue(new Error('network down'));

    await handleComputeExperiment('task-1', { ...VALID_PAYLOAD }, TASK, '', 'o', 'r', null, { value: '' });

    const completion = lastCompletionPayload();
    expect(completion.status).toBe('FAILED');
    expect(completion.failure_category).toBe('compute_monitor_failed');
  });
});
