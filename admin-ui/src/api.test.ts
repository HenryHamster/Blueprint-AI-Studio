import { afterEach, describe, expect, it, vi } from 'vitest';
import { previewOnboardProject, createComputeExperiment } from './api';

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
