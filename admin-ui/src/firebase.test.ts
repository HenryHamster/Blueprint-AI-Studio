import { describe, expect, it, vi, afterEach } from 'vitest';

describe('firebase auth bootstrap', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('surfaces an explicit setup error for placeholder Firebase config', async () => {
    vi.stubEnv('VITE_DASHBOARD_API_TOKEN', '');
    const firebase = await import('./firebase');

    await expect(firebase.getIdToken()).rejects.toThrow(/valid web API key/);

    const onAuthChange = vi.fn();
    const onError = vi.fn();
    const unsubscribe = firebase.onAuthChange(onAuthChange, onError);

    await vi.waitFor(() => {
      expect(onError).toHaveBeenCalledWith(expect.stringContaining('valid web API key'));
    });
    expect(onAuthChange).toHaveBeenCalledWith(null);

    unsubscribe();
  });

  it('uses the dashboard token local-access mode without Firebase auth', async () => {
    vi.stubEnv('VITE_DASHBOARD_API_TOKEN', 'local-dashboard-token');
    const firebase = await import('./firebase');

    await expect(firebase.getIdToken()).resolves.toBe('local-dashboard-token');

    const onAuthChange = vi.fn();
    const onError = vi.fn();
    const unsubscribe = firebase.onAuthChange(onAuthChange, onError);

    await vi.waitFor(() => {
      expect(onAuthChange).toHaveBeenCalledWith(
        expect.objectContaining({ email: 'dashboard-agent@system.local' }),
      );
    });
    expect(onError).not.toHaveBeenCalled();

    unsubscribe();
  });
});
