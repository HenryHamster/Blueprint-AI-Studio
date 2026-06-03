import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { onboardProject } from './api';

vi.mock('./firebase', () => ({
  getIdToken: vi.fn(async () => 'test-token'),
}));

describe('onboardProject', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn(async () => new Response(JSON.stringify({
      project_id: 'project-123',
      build_config: {},
    }), { status: 200 }));
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('serializes structured steering and omits blank fields', async () => {
    await onboardProject(
      'acme',
      'platform',
      undefined,
      undefined,
      undefined,
      'Ship the fix',
      {
        context: {
          project_type: ' startup ',
          users: ' ',
          team: '4 engineers',
          deploy_cadence: '',
          notes: 'Prioritize reliability',
        },
        constraints: ['  Do not change workflows  ', ''],
      },
    );

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    const requestPath = String(url).startsWith('http') ? new URL(String(url)).pathname : String(url);
    expect(requestPath).toBe('/api/admin/projects/onboard');
    const body = JSON.parse(String(init?.body));
    expect(body).toEqual({
      github_owner: 'acme',
      github_repo: 'platform',
      goal: 'Ship the fix',
      initial_steering: {
        context: {
          project_type: 'startup',
          team: '4 engineers',
          notes: 'Prioritize reliability',
        },
        constraints: ['Do not change workflows'],
      },
    });
  });

  it('omits initial_steering when every structured field is blank', async () => {
    await onboardProject(
      'acme',
      'platform',
      undefined,
      undefined,
      undefined,
      undefined,
      {
        context: {
          project_type: ' ',
          users: '',
          team: '',
          deploy_cadence: ' ',
          notes: ' ',
        },
        constraints: ['   '],
      },
    );

    const [, init] = fetchSpy.mock.calls[0];
    const body = JSON.parse(String(init?.body));
    expect(body).toEqual({
      github_owner: 'acme',
      github_repo: 'platform',
    });
    expect(body.initial_steering).toBeUndefined();
  });
});
