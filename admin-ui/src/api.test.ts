import { afterEach, describe, expect, it, vi } from 'vitest';
import { previewOnboardProject } from './api';

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
