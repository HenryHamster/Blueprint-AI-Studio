import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  previewOnboardProject,
  createComputeExperiment,
  fetchComputeJobs,
  fetchComputeJobsPaged,
  analyzeComputeJob,
  retryProjectBootstrap,
  resolveViewportFailureDownloadUrl,
  fetchImprovementLanes,
  fetchImprovementLaneDetail,
  fetchAlerts,
  fetchProjectAlertDomains,
  updateProjectAlertDomains,
} from './api';

vi.mock('./firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
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
    expect(String(url)).toContain('/api/admin/projects/onboard');
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      github_owner: 'acme',
      github_repo: 'repo',
      preview_only: true,
      build_config_override: { requires_ci_checks: false },
    });
  });
});

describe('fetchAlerts', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('serializes optional alert metadata filters', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    );

    await expect(fetchAlerts({
      projectId: 'proj-1',
      domain: 'sr_quality',
      eventType: 'failure_alert',
      source: 'orchestrator',
    })).resolves.toEqual([]);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/admin/alerts?');
    expect(url).toContain('project_id=proj-1');
    expect(url).toContain('domain=sr_quality');
    expect(url).toContain('event_type=failure_alert');
    expect(url).toContain('source=orchestrator');
  });
});

describe('project alert-domain config API', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fetches alert domains for a project', async () => {
    const body = { project_id: 'proj-1', domains: [] };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 }),
    );

    await expect(fetchProjectAlertDomains('proj-1')).resolves.toEqual(body);

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/projects/proj-1/alert-domains');
  });

  it('patches alert domain enablement', async () => {
    const body = { project_id: 'proj-1', domains: [] };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 }),
    );

    await expect(updateProjectAlertDomains('proj-1', { warm_pool: { enabled: false } })).resolves.toEqual(body);

    const [, init] = fetchMock.mock.calls[0];
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/projects/proj-1/alert-domains');
    expect((init as RequestInit).method).toBe('PATCH');
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      domains: { warm_pool: { enabled: false } },
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
    expect(String(url)).toContain('/api/admin/tasks/compute-experiment');
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

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/projects/proj-1/bootstrap/retry');
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('POST');
  });

  it('throws the backend message on a non-2xx response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'project is not eligible' }), { status: 409 }),
    );

    await expect(retryProjectBootstrap('proj-1')).rejects.toThrow('project is not eligible');
  });
});

describe('resolveViewportFailureDownloadUrl', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('serializes the stable viewport screenshot lookup query and returns the URL', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ url: 'https://storage.example/signed.png' }), { status: 200 }),
    );

    await expect(resolveViewportFailureDownloadUrl('proj-1', 'Messages Panel', 375)).resolves.toBe(
      'https://storage.example/signed.png',
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      '/api/admin/viewport-failures/download-url?project_id=proj-1&component=Messages+Panel&width=375',
    );
  });

  it('throws the backend detail when screenshot resolution fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Screenshot not found' }), { status: 404 }),
    );

    await expect(
      resolveViewportFailureDownloadUrl('proj-1', 'MessagesPanel', 375),
    ).rejects.toThrow('Screenshot not found');
  });

  it('rejects an empty resolver payload', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ url: '' }), { status: 200 }),
    );

    await expect(
      resolveViewportFailureDownloadUrl('proj-1', 'MessagesPanel', 375),
    ).rejects.toThrow('Viewport screenshot resolver returned an empty URL');
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
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/projects/proj-1/compute-jobs');
  });

  it('serializes include_finished_minutes when provided', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ compute_jobs: [] }), { status: 200 }),
    );

    await fetchComputeJobs('proj-1', undefined, 1440);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/projects/proj-1/compute-jobs?include_finished_minutes=1440');
  });
});

describe('fetchComputeJobsPaged', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('omits all_terminal when opts not provided', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ compute_jobs: [], has_more: false }), { status: 200 }),
    );

    await fetchComputeJobsPaged('proj-1');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/admin/projects/proj-1/compute-jobs');
    expect(url).not.toContain('all_terminal');
  });

  it('includes all_terminal=true when opts.allTerminal is set', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ compute_jobs: [], has_more: true }), { status: 200 }),
    );

    const result = await fetchComputeJobsPaged('proj-1', undefined, { allTerminal: true });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain('all_terminal=true');
    expect(result.hasMore).toBe(true);
    expect(result.jobs).toEqual([]);
  });

  it('forwards the summary object when present in the response', async () => {
    const mockSummary = {
      total_jobs: 5,
      active_jobs: 1,
      terminal_jobs: 4,
      total_spend_usd: 123.45,
      outcome_breakdown: { 'running': 1, 'succeeded': 4 },
      criterion_measured: 4,
      criterion_met: 3,
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        compute_jobs: [],
        has_more: false,
        summary: mockSummary,
      }), { status: 200 }),
    );

    const result = await fetchComputeJobsPaged('proj-1');

    expect(result.summary).toEqual(mockSummary);
    expect(result.summary?.total_jobs).toBe(5);
    expect(result.summary?.total_spend_usd).toBe(123.45);
    expect(result.summary?.criterion_met).toBe(3);
    expect(result.summary?.criterion_measured).toBe(4);
  });

  it('falls back to empty summary when summary is absent in the response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ compute_jobs: [], has_more: false }), { status: 200 }),
    );

    const result = await fetchComputeJobsPaged('proj-1');

    expect(result.summary).toEqual({
      total_jobs: 0, active_jobs: 0, terminal_jobs: 0, total_spend_usd: 0,
      outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0,
    });
  });
});

describe('fetchImprovementLanes', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('serializes the project paging query and preserves additive response fields', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        lanes: [],
        total_returned: 0,
        offset: 5,
        limit: 25,
        project_id: 'proj-1',
        extra_banner: 'shadow-mode',
      }), { status: 200 }),
    );

    const result = await fetchImprovementLanes('proj-1', 25, 5);

    expect(result).toMatchObject({
      total_returned: 0,
      offset: 5,
      limit: 25,
      project_id: 'proj-1',
      extra_banner: 'shadow-mode',
    });
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/improvement-lanes?project_id=proj-1&limit=25&offset=5');
    expect((fetchMock.mock.calls[0][1] as RequestInit | undefined)?.method).toBeUndefined();
  });

  it('throws the backend status on non-2xx responses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'project not found' }), { status: 404 }),
    );

    await expect(fetchImprovementLanes('proj-1')).rejects.toThrow('Failed to fetch improvement lanes: 404');
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

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/compute-jobs/job-1/analyze');
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

describe('fetchImprovementLaneDetail', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds the correct URL with project_id param and encodes the lane id', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ id: 'lane/special id', checkpoints: [], manifest: {}, current_task: null }), { status: 200 }),
    );

    await fetchImprovementLaneDetail('proj-1', 'lane/special id');

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/admin/improvement-lanes/lane%2Fspecial%20id');
    expect(url).toContain('project_id=proj-1');
  });

  it('throws on non-ok responses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'not found' }), { status: 404 }),
    );

    await expect(fetchImprovementLaneDetail('proj-1', 'lane-1')).rejects.toThrow(
      'Failed to fetch improvement lane detail: 404',
    );
  });
});
