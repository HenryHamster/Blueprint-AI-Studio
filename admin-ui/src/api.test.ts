import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  previewOnboardProject,
  createComputeExperiment,
  fetchComputeJobs,
  analyzeComputeJob,
  retryProjectBootstrap,
} from './api';

vi.mock('./firebase', () => ({
  getIdToken: vi.fn(async () => 'test-token'),
}));

describe('previewOnboardProject', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('serializes preview_only and the override payload', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ build_config: {}, default_branch: 'main' }), { status: 200 }),
    );

    await previewOnboardProject('acme', 'repo', undefined, { requires_ci_checks: false });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/admin/projects/onboard');
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      github_owner: 'acme',
      github_repo: 'repo',
      preview_only: true,
      build_config_override: { requires_ci_checks: false },
    });
  });
});

describe('createComputeExperiment', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('POSTs the launch contract to the dedicated endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ id: 'task-ce-1' }), { status: 200 }),
    );

    const result = await createComputeExperiment({
      github_repo: 'acme/repo',
      base_branch: 'main',
      title: 'experiment',
      prompt: 'run it',
      command: 'python train.py',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
    });

    expect(result).toEqual({ id: 'task-ce-1' });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/admin/tasks/compute-experiment');
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse(String((init as RequestInit).body))).toMatchObject({
      command: 'python train.py',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
    });
  });

  it('throws the backend error message on a non-2xx response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'spend cap exceeds approval threshold' }), { status: 400 }),
    );

    await expect(
      createComputeExperiment({
        github_repo: 'acme/repo',
        base_branch: 'main',
        title: 't',
        prompt: 'p',
        command: 'python x.py',
        spend_cap_usd: 999,
        deadline_seconds: 1800,
      }),
    ).rejects.toThrow('spend cap exceeds approval threshold');
  });
});

describe('retryProjectBootstrap', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('POSTs to the bootstrap retry endpoint and returns the structured outcome', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ state: 'spawned', task_id: 'task-1', reason: 'operator_retry' }), { status: 200 }),
    );

    await expect(retryProjectBootstrap('proj-1')).resolves.toEqual({
      state: 'spawned',
      task_id: 'task-1',
      reason: 'operator_retry',
    });

    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/projects/proj-1/bootstrap/retry');
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('POST');
  });

  it('throws the backend message on a non-2xx response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'project is not eligible' }), { status: 409 }),
    );

    await expect(retryProjectBootstrap('proj-1')).rejects.toThrow('project is not eligible');
  });
});

describe('fetchComputeJobs', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('omits include_finished_minutes unless explicitly provided', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ compute_jobs: [] }), { status: 200 }),
    );

    await fetchComputeJobs('proj-1');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/projects/proj-1/compute-jobs');
  });

  it('serializes include_finished_minutes when provided', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ compute_jobs: [] }), { status: 200 }),
    );

    await fetchComputeJobs('proj-1', undefined, 1440);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/projects/proj-1/compute-jobs?include_finished_minutes=1440');
  });
});

describe('analyzeComputeJob', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('posts to the analyze endpoint and preserves upgraded envelopes', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'upgraded', analysis_mode: 'llm', extra: 'field' }), { status: 200 }),
    );

    await expect(analyzeComputeJob('job-1')).resolves.toMatchObject({
      status: 'upgraded',
      analysis_mode: 'llm',
      extra: 'field',
    });

    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/compute-jobs/job-1/analyze');
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('POST');
  });

  it('returns analysis_unavailable envelopes with a reason', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'analysis_unavailable', reason: 'analysis-disabled' }), { status: 200 }),
    );

    await expect(analyzeComputeJob('job-2')).resolves.toMatchObject({
      status: 'analysis_unavailable',
      reason: 'analysis-disabled',
    });
  });
});
