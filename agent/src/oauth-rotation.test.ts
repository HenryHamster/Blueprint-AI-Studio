import { vi, describe, it, expect, beforeEach } from 'vitest';

const { mockReadFileSync, mockWriteFileSync, mockExistsSync } = vi.hoisted(() => {
  const mockReadFileSync = vi.fn();
  const mockWriteFileSync = vi.fn();
  const mockExistsSync = vi.fn().mockReturnValue(false);
  process.env.PROVIDER = 'openai';
  process.env.AUTH_MODE = 'oauth';
  return { mockReadFileSync, mockWriteFileSync, mockExistsSync };
});

vi.mock('fs', () => ({
  default: {
    readFileSync: mockReadFileSync,
    writeFileSync: mockWriteFileSync,
    existsSync: mockExistsSync,
  },
}));

import { recordInitialOAuthAuthHash, rotateOAuthCredentialsIfChanged, ClaudeTimeoutError } from './oauth-rotation.js';
import { refreshRuntimeState } from './providers/runners.js';

beforeEach(() => {
  vi.clearAllMocks();
  mockExistsSync.mockReturnValue(false);
  mockReadFileSync.mockReset();
  mockWriteFileSync.mockReset();
  process.env.PROVIDER = 'openai';
  process.env.AUTH_MODE = 'oauth';
  refreshRuntimeState();
  recordInitialOAuthAuthHash();
  mockReadFileSync.mockClear();
});

describe('ClaudeTimeoutError', () => {
  it('has correct name and failureCategory', () => {
    const err = new ClaudeTimeoutError('timed out');
    expect(err.name).toBe('ClaudeTimeoutError');
    expect(err.failureCategory).toBe('timeout');
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toBe('timed out');
  });
});

describe('recordInitialOAuthAuthHash', () => {
  it('does not throw when auth file is missing', () => {
    mockReadFileSync.mockImplementation(() => { throw new Error('ENOENT'); });
    expect(() => { recordInitialOAuthAuthHash(); }).not.toThrow();
  });
});

describe('rotateOAuthCredentialsIfChanged', () => {
  it('skips when auth file does not exist', async () => {
    mockExistsSync.mockReturnValue(false);
    const apiPostFn = vi.fn();
    await rotateOAuthCredentialsIfChanged('task-1', apiPostFn);
    expect(apiPostFn).not.toHaveBeenCalled();
  });

  it('calls apiPostFn when auth file exists and hash changed', async () => {
    mockExistsSync.mockReturnValue(true);
    // readFileSync for hash (Buffer) and for contents (string)
    mockReadFileSync
      .mockReturnValueOnce(Buffer.from('new-token-data'))
      .mockReturnValueOnce('new-token-data');
    const apiPostFn = vi.fn().mockResolvedValue({ rotated: true });
    // recordInitialOAuthAuthHash sets initial hash to something different (null = no prior hash)
    await rotateOAuthCredentialsIfChanged('task-1', apiPostFn);
    expect(apiPostFn).toHaveBeenCalledWith(
      '/api/agent/credentials/rotate',
      expect.objectContaining({ task_id: 'task-1' }),
    );
  });

  it('posts the credential version it started from', async () => {
    mockReadFileSync.mockReturnValueOnce(Buffer.from('old-token-data'));
    recordInitialOAuthAuthHash();
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync
      .mockReturnValueOnce(Buffer.from('new-token-data'))
      .mockReturnValueOnce('new-token-data');
    const apiPostFn = vi.fn().mockResolvedValue({ rotated: true });

    await rotateOAuthCredentialsIfChanged('task-cas', apiPostFn);

    expect(apiPostFn).toHaveBeenCalledWith(
      '/api/agent/credentials/rotate',
      expect.objectContaining({
        task_id: 'task-cas',
        base_hash: '2ff6867f3bbcdad12fd175a0ace50a541baf9a93f1d3b253630f1a3c15d50f6b',
      }),
    );
  });

  it('adopts the authoritative credential when another VM wins rotation', async () => {
    mockReadFileSync.mockReturnValueOnce(Buffer.from('old-token-data'));
    recordInitialOAuthAuthHash();
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync
      .mockReturnValueOnce(Buffer.from('losing-token-data'))
      .mockReturnValueOnce('losing-token-data')
      .mockReturnValueOnce(Buffer.from('winner-token-data'));
    const apiPostFn = vi.fn().mockResolvedValue({
      rotated: false,
      reason: 'stale_credential',
      secret: 'winner-token-data',
    });

    await rotateOAuthCredentialsIfChanged('task-loser', apiPostFn);

    expect(mockWriteFileSync).toHaveBeenCalledWith(
      '/opt/agent/openai/auth.json',
      'winner-token-data',
      { encoding: 'utf-8', mode: 0o600 },
    );
  });

  it('posts taskless rotations for pool pre-claim validation', async () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync
      .mockReturnValueOnce(Buffer.from('pool-token-data'))
      .mockReturnValueOnce('pool-token-data');
    const apiPostFn = vi.fn().mockResolvedValue({ rotated: true });

    await rotateOAuthCredentialsIfChanged('', apiPostFn);

    expect(apiPostFn).toHaveBeenCalledWith(
      '/api/agent/credentials/rotate',
      expect.objectContaining({ task_id: '', secret: 'pool-token-data' }),
    );
  });

  it('does not advance the baseline when the backend reports a no-op', async () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync
      .mockReturnValueOnce(Buffer.from('first-token-data'))
      .mockReturnValueOnce('first-token-data')
      .mockReturnValueOnce(Buffer.from('first-token-data'))
      .mockReturnValueOnce('first-token-data');
    const apiPostFn = vi.fn()
      .mockResolvedValueOnce({ rotated: false, reason: 'pool VM has no LLM runtime context' })
      .mockResolvedValueOnce({ rotated: true });

    await expect(rotateOAuthCredentialsIfChanged('', apiPostFn)).rejects.toThrow(
      /did not persist/,
    );
    await rotateOAuthCredentialsIfChanged('', apiPostFn);

    expect(apiPostFn).toHaveBeenCalledTimes(2);
  });

  it('follows refreshed provider/auth runtime after a retarget', async () => {
    process.env.PROVIDER = 'anthropic';
    process.env.AUTH_MODE = 'api_key';
    refreshRuntimeState();

    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue('should-not-be-read');
    const apiPostFn = vi.fn();
    await rotateOAuthCredentialsIfChanged('task-anthropic', apiPostFn);
    expect(apiPostFn).not.toHaveBeenCalled();
    expect(mockReadFileSync).not.toHaveBeenCalled();

    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    refreshRuntimeState();

    mockExistsSync.mockReturnValue(true);
    mockReadFileSync
      .mockReturnValueOnce(Buffer.from('retargeted-token-data'))
      .mockReturnValueOnce('retargeted-token-data');
    apiPostFn.mockResolvedValueOnce({ rotated: true });
    await rotateOAuthCredentialsIfChanged('task-openai', apiPostFn);
    expect(apiPostFn).toHaveBeenCalledWith(
      '/api/agent/credentials/rotate',
      expect.objectContaining({ task_id: 'task-openai' }),
    );
  });
});
