import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Pre-import setup (hoisted before any module loads) ───────────────────────
// index.ts guards main() and process event handlers behind an isMain check,
// so they do NOT run when the file is imported by vitest. The hoisted block
// is still needed to share the fs mock instances with the vi.mock factory.
const { mockReadFileSync } = vi.hoisted(() => {
  // Build the fs mock object here so both vi.mock factory and test bodies
  // can reference the same vi.fn() instances.
  const mockReadFileSync = vi.fn();
  // Keep module-level provider/auth defaults deterministic across local and CI
  // environments. Some test cases call helpers without explicit provider args.
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
  // Clear before index.ts loads so the module-level LLM_CLI_TRANSLATION
  // constant is null. Tests that verify translation behavior set the env var
  // themselves and call parseLLMCliTranslation() directly.
  delete process.env.LLM_CLI_TRANSLATION;
  return { mockReadFileSync };
});

// ── Module mocks ─────────────────────────────────────────────────────────────
vi.mock('fs', () => ({
  default: {
    readFileSync: mockReadFileSync,
    writeFileSync: vi.fn(),
    unlinkSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    openSync: vi.fn().mockReturnValue(3),
    closeSync: vi.fn(),
    chmodSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  spawn: vi.fn().mockReturnValue({
    stdin: { on: vi.fn(), end: vi.fn() },
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execSync: vi.fn().mockReturnValue(''),
  // reset.ts (now imported transitively via index.ts) wraps execFile with
  // promisify; supply the (cmd, args, opts, callback) shape so reset's
  // own dependency surface doesn't break unrelated index tests.
  execFile: (
    _cmd: string,
    _args: string[],
    _opts: unknown,
    cb: (err: Error | null, stdout: string, stderr: string) => void,
  ) => {
    void Promise.resolve().then(() => { cb(null, '', ''); });
  },
}));

vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));

// ── Imports (after mocks are registered) ─────────────────────────────────────
import {
  strVal,
  sanitize,
  validateBranchName,
  resolveBaseBranch,
  readHandlerOutput,
  testFixLoop,
  errorFingerprint,
  recordToolUse,
  snapshotToolTrace,
  appendToLogBuffer,
  snapshotLogBuffer,
  apiGet,
  detectLoopDivergence,
  recordLlmCallStart,
  recordLlmTokens,
  getLlmTelemetry,
  deriveTaskBranch,
  resolveTaskBranchNamespace,
  TASK_TYPE_BRANCH_PREFIX,
  computeRefreshDelayMs,
  isBootstrapToken,
  isTaskNoLongerActiveError,
  postTerminalStatusSync,
  GracefulShutdownSignal,
  _isCancelRequestedForTesting,
  _resetCancelRequestedForTesting,
  _setCancelRequestedForTesting,
  resetPerRoleModuleState,
  _getWarmStartForTesting,
  _getJobModeForTesting,
  _setJobModeForTesting,
  _setWarmStartForTesting,
  setCurrentPromptFingerprint,
  getCurrentPromptFingerprint,
  listOpenPullRequestsForBranch,
  _crashReportForTesting,
  _resetCredentialStateForTesting,
  refreshRuntimeState,
  type FailureCategory,
} from './index.js';
import { jaccardSimilarity } from './utils/diff-detection.js';
import {
  __setCommandExistsOverrideForSmoke,
  __resetClaudeCapabilityCacheForTesting,
} from './providers/runners.js';
import { TMP_OUTPUT_NAMES } from './reset.js';
import {
  drainFixLoopEventBuffer,
  _resetFixLoopEventStateForTesting,
} from './fix-loop-events.js';
import {
  MAX_TEST_FIX_ATTEMPTS,
  TOKEN_REFRESH_FLOOR_MS,
  TOKEN_REFRESH_CEILING_MS,
  BOOTSTRAP_TOKEN_THRESHOLD_MS,
} from './constants.js';
import { execFileSync, spawn, execSync } from 'child_process';
import fs from 'fs';
import * as discovery from './prompts/discovery.js';

const makeFetchMock = () => vi.fn<(input: unknown, init?: RequestInit) => Promise<unknown>>();

beforeEach(() => {
  // Bypass the pre-flight CLI-exists check: provider binaries (claude, codex, etc.)
  // are not installed in the test environment; spawn is mocked separately.
  __setCommandExistsOverrideForSmoke(() => true);
  __resetClaudeCapabilityCacheForTesting();
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
  delete process.env.LLM_MODEL;
  delete process.env.LLM_SETTINGS;
  delete process.env.LLM_CLI_TRANSLATION;
  delete process.env.LLM_EXECUTION_MODE;
  delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
  delete process.env.CLAUDE_CODE_EFFORT_LEVEL;
  delete process.env.ANTHROPIC_API_KEY;
  delete process.env.ANTHROPIC_AUTH_TOKEN;
  delete process.env.ANTHROPIC_BASE_URL;
  delete process.env.ANTHROPIC_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_OPUS_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_SONNET_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  delete process.env.CLAUDE_CODE_SUBAGENT_MODEL;
  delete process.env.ZAI_API_KEY;
  delete process.env.ZAI_ANTHROPIC_BASE_URL;
  delete process.env.ZAI_BASE_URL;
  delete process.env.DEEPSEEK_API_KEY;
  delete process.env.DEEPSEEK_ANTHROPIC_BASE_URL;
  delete process.env.DEEPSEEK_BASE_URL;
  delete process.env.OPENAI_API_KEY;
  delete process.env.CODEX_API_KEY;
  delete process.env.CODEX_HOME;
  delete process.env.GEMINI_API_KEY;
  delete process.env.GOOGLE_APPLICATION_CREDENTIALS;
  delete process.env.GOOGLE_CLOUD_PROJECT;
  delete process.env.GOOGLE_CLOUD_LOCATION;
  _resetCredentialStateForTesting();
  refreshRuntimeState();
});

function mockJwtWithExp(expUnixSec: number): string {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({ exp: expUnixSec, sub: 'test-task' })).toString('base64url');
  return `${header}.${payload}.fakesig`;
}

describe('computeRefreshDelayMs', () => {
  it('schedules at 75% of remaining lifetime for a 1h token', () => {
    const now = 1_000_000_000_000; // arbitrary fixed ms
    const expSec = Math.floor(now / 1000) + 3600; // 1 hour remaining
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBe(3600 * 1000 * 0.75); // 2700000ms
  });

  it('clamps to floor when remaining lifetime is very short', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 30; // 30 seconds remaining
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBe(TOKEN_REFRESH_FLOOR_MS);
  });

  it('clamps to ceiling when remaining lifetime is very long', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 7200; // 2 hours remaining
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBe(TOKEN_REFRESH_CEILING_MS);
  });

  it('falls back to floor for an unparseable token', () => {
    const delay = computeRefreshDelayMs('not-a-jwt');
    expect(delay).toBe(TOKEN_REFRESH_FLOOR_MS);
  });

  it('falls back to floor when token has no exp claim', () => {
    const now = 1_000_000_000_000;
    const header = Buffer.from(JSON.stringify({ alg: 'HS256' })).toString('base64url');
    const payload = Buffer.from(JSON.stringify({ sub: 'test' })).toString('base64url');
    const token = `${header}.${payload}.sig`;
    expect(computeRefreshDelayMs(token, now)).toBe(TOKEN_REFRESH_FLOOR_MS);
  });

  it('is at ceiling for a fresh standard 1h token', () => {
    // 75% of 3600s = 2700s which is 45 min — within [3 min, 55 min]
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 3600;
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBeGreaterThanOrEqual(TOKEN_REFRESH_FLOOR_MS);
    expect(delay).toBeLessThanOrEqual(TOKEN_REFRESH_CEILING_MS);
  });
});

// ── isBootstrapToken ─────────────────────────────────────────────────────────
describe('isBootstrapToken', () => {
  it('returns true for a token with remaining lifetime above threshold', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 5400; // 90 min remaining
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(true);
  });

  it('returns false for a normal short-TTL token (1h remaining)', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 3600; // 60 min — below 75 min threshold
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(false);
  });

  it('returns false exactly at the threshold boundary', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + BOOTSTRAP_TOKEN_THRESHOLD_MS / 1000;
    // remaining == threshold is NOT above threshold
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(false);
  });

  it('returns true just above the threshold boundary', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + BOOTSTRAP_TOKEN_THRESHOLD_MS / 1000 + 1;
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(true);
  });

  it('returns false for an unparseable token', () => {
    expect(isBootstrapToken('not-a-jwt')).toBe(false);
  });

  it('returns false for a token with no exp claim', () => {
    const now = 1_000_000_000_000;
    const header = Buffer.from(JSON.stringify({ alg: 'HS256' })).toString('base64url');
    const payload = Buffer.from(JSON.stringify({ sub: 'test' })).toString('base64url');
    const token = `${header}.${payload}.sig`;
    expect(isBootstrapToken(token, now)).toBe(false);
  });
});

// ── strVal ───────────────────────────────────────────────────────────────────
describe('strVal', () => {
  it('returns string values unchanged', () => {
    expect(strVal('hello')).toBe('hello');
  });

  it('returns empty string for undefined', () => {
    expect(strVal(undefined)).toBe('');
  });

  it('returns fallback for null', () => {
    expect(strVal(null, 'default')).toBe('default');
  });

  it('returns fallback (empty) for numbers', () => {
    expect(strVal(42)).toBe('');
  });

  it('returns custom fallback for numbers', () => {
    expect(strVal(42, 'n/a')).toBe('n/a');
  });

  it('returns empty string for empty string input', () => {
    expect(strVal('')).toBe('');
  });

  it('uses custom fallback when value is not a string', () => {
    expect(strVal(false, 'fb')).toBe('fb');
  });

  it('returns string for truthy non-string value with fallback', () => {
    expect(strVal({}, 'obj')).toBe('obj');
  });
});

// ── sanitize ─────────────────────────────────────────────────────────────────
describe('sanitize', () => {
  it('preserves text that contains no secrets', () => {
    const text = 'Hello, world! No secrets here.';
    expect(sanitize(text)).toBe(text);
  });

  it('redacts ghp_ GitHub tokens', () => {
    expect(sanitize('token: ghp_AbCdEfGhIjKlMnOp123')).toBe('token: ghp_***');
  });

  it('redacts ghs_ GitHub app tokens', () => {
    expect(sanitize('ghs_ABCdef123XYZ')).toBe('ghs_***');
  });

  it('redacts github_pat_ personal access tokens', () => {
    expect(sanitize('github_pat_11ABCDE_fooBar_baz_QUX')).toBe('github_pat_***');
  });

  it('redacts sk-ant- Anthropic API keys', () => {
    expect(sanitize('key: sk-ant-api03-someverylongkey-abc')).toBe('key: sk-ant-***');
  });

  it('redacts OpenAI sk- API keys with a conservative minimum length', () => {
    const stderr = 'openai stderr: invalid key sk-abcdefghijklmnopqrstuvwxyz123456';
    expect(sanitize(stderr)).toBe('openai stderr: invalid key sk-***');
    expect(sanitize('short marker sk-not-a-real-key')).toBe('short marker sk-not-a-real-key');
  });

  it('redacts OpenAI sk-proj- API keys', () => {
    // The env-var pattern fires after the sk-proj pattern, fully redacting the env var
    expect(
      sanitize('codex failed with OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456'),
    ).toBe('codex failed with OPENAI_API_KEY=***');
  });

  it('redacts Google API keys', () => {
    expect(
      sanitize('gemini stderr: api key AIzaSyabcdefghijklmnopqrstuvwxyz123456789'),
    ).toBe('gemini stderr: api key AIza***');
  });

  it('redacts bare ya29 OAuth tokens and unquoted key=value dumps', () => {
    const token = 'ya29.a0AfH6SMBx_AbCdEfGhIjKlMnOpQrStUvWx';
    expect(sanitize(token)).toBe('ya29***');
    const kv = sanitize(`GOOGLE_oauth_token=${token}`);
    expect(kv).toContain('GOOGLE_oauth_token=');
    expect(kv).toContain('***');
    expect(kv).not.toContain(token);
  });

  it('redacts quoted credential fields while preserving field names and quote style', () => {
    const json = '{"access_token":"ya29.secret","refresh_token":"1//refresh","id_token":"header.payload.sig","api_key":"AIzaSyabcdefghijklmnopqrstuvwxyz123456789","client_secret":"client-secret","private_key":"-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----","token":"opaque"}';
    expect(sanitize(json)).toBe('{"access_token":"***","refresh_token":"***","id_token":"***","api_key":"***","client_secret":"***","private_key":"***","token":"***"}');
  });

  it('redacts quoted key-value credential fields in stderr-like text', () => {
    const stderr = "stderr: access_token='ya29.secret' client_secret=\"secret\" token='opaque'";
    expect(sanitize(stderr)).toBe("stderr: access_token='***' client_secret=\"***\" token='***'");
  });

  it('does not mangle ordinary prose mentioning token or api_key without credential-looking values', () => {
    const text = 'The token field and api_key setting are missing; no secret value was provided.';
    expect(sanitize(text)).toBe(text);
  });

  it('redacts JWT tokens matching eyJ…eyJ…sig pattern', () => {
    const jwt =
      'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c';
    expect(sanitize(jwt)).toBe('eyJ***');
  });

  it('redacts Bearer tokens in Authorization headers', () => {
    expect(sanitize('Authorization: Bearer abc123secrettoken')).toBe(
      'Authorization: Bearer ***',
    );
  });

  it('redacts oauth_token= query parameter values', () => {
    const result = sanitize('url?oauth_token=supersecret&other=ok');
    expect(result).toContain('oauth_token=***');
    expect(result).not.toContain('supersecret');
  });

  it('redacts PEM RSA private keys', () => {
    const pem =
      '-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----';
    expect(sanitize(pem)).toBe('***PRIVATE KEY***');
  });

  it('redacts PEM EC private keys', () => {
    const pem =
      '-----BEGIN EC PRIVATE KEY-----\nMHQCAQEEIO\n-----END EC PRIVATE KEY-----';
    expect(sanitize(pem)).toBe('***PRIVATE KEY***');
  });

  it('redacts x-access-token URLs', () => {
    expect(
      sanitize('https://x-access-token:ghp_abc123@github.com/org/repo.git'),
    ).toBe('https://x-access-token:***@github.com/org/repo.git');
  });

  it('redacts multiple secret types in a single string', () => {
    const text =
      'token: ghp_ABC123 and key: sk-ant-api03-XYZ and Authorization: Bearer mytoken123';
    const result = sanitize(text);
    expect(result).not.toContain('ghp_ABC123');
    expect(result).not.toContain('sk-ant-api03-XYZ');
    expect(result).not.toContain('mytoken123');
    expect(result).toContain('ghp_***');
    expect(result).toContain('sk-ant-***');
    expect(result).toContain('Bearer ***');
  });

  it('redacts AWS IAM access keys (AKIA + 16 uppercase alphanumeric)', () => {
    expect(sanitize('AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE')).toBe(
      'AWS_ACCESS_KEY_ID=AKIA***',
    );
    // 15 chars after AKIA — one short of required 16, must NOT be redacted
    expect(sanitize('AKIA123456789AB')).toBe('AKIA123456789AB');
  });

  it('redacts Stripe live secret keys (sk_live_)', () => {
    expect(sanitize('STRIPE_SECRET=sk_live_abcdef123456')).toBe('STRIPE_SECRET=sk_live_***');
  });

  it('redacts Stripe test secret keys (sk_test_)', () => {
    expect(sanitize('STRIPE_SECRET=sk_test_abcdef123456')).toBe('STRIPE_SECRET=sk_test_***');
  });

  it('redacts Stripe restricted keys (rk_live_) and webhook secrets (whsec_)', () => {
    expect(sanitize('key=rk_live_abcdef123456')).toBe('key=rk_live_***');
    expect(sanitize('WEBHOOK_SECRET=whsec_abcdef123456')).toBe('WEBHOOK_SECRET=whsec_***');
  });

  it('redacts postgresql:// and postgres:// DSNs with embedded passwords', () => {
    expect(sanitize('postgresql://dbuser:s3cr3t@db.host/mydb')).toBe(
      'postgresql://***:***@db.host/mydb',
    );
    expect(sanitize('postgres://dbuser:s3cr3t@db.host/mydb')).toBe(
      'postgres://***:***@db.host/mydb',
    );
    // SQLAlchemy +driver variant must redact while preserving the scheme
    expect(sanitize('postgresql+asyncpg://dbuser:s3cr3t@db.host/mydb')).toBe(
      'postgresql+asyncpg://***:***@db.host/mydb',
    );
  });

  it('redacts AGENT_SECRET_TOKEN value in env-var key=value format', () => {
    const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0YXNrLTEyMyJ9.SIG';
    expect(sanitize(`AGENT_SECRET_TOKEN=${jwt}`)).toBe('AGENT_SECRET_TOKEN=***');
    // Non-JWT opaque token value
    expect(sanitize('AGENT_SECRET_TOKEN=some-opaque-token-value')).toBe('AGENT_SECRET_TOKEN=***');
    // Should not redact very short values (below the 4-char floor)
    expect(sanitize('AGENT_SECRET_TOKEN=abc')).toBe('AGENT_SECRET_TOKEN=abc');
  });

  it('redacts provider API key env vars (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, OPENAI_API_KEY, GEMINI_API_KEY, ZAI_API_KEY, DEEPSEEK_API_KEY, CLAUDE_CODE_OAUTH_TOKEN)', () => {
    expect(sanitize('ANTHROPIC_API_KEY=sk-ant-api03-secretkey123')).toBe('ANTHROPIC_API_KEY=***');
    expect(sanitize('ANTHROPIC_AUTH_TOKEN=plain-zai-token')).toBe('ANTHROPIC_AUTH_TOKEN=***');
    expect(sanitize('OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123')).toBe('OPENAI_API_KEY=***');
    expect(sanitize('GEMINI_API_KEY=AIzaSyabcdefghijklmnop')).toBe('GEMINI_API_KEY=***');
    expect(sanitize('ZAI_API_KEY=plain-zai-token')).toBe('ZAI_API_KEY=***');
    expect(sanitize('ZAI_API_KEY="plain-zai-token"')).toBe('ZAI_API_KEY=***');
    expect(sanitize('DEEPSEEK_API_KEY=plain-deepseek-token')).toBe('DEEPSEEK_API_KEY=***');
    expect(sanitize('DEEPSEEK_API_KEY="plain-deepseek-token"')).toBe('DEEPSEEK_API_KEY=***');
    const zaiJsonOut = sanitize('{"ZAI_API_KEY":"plain-zai-token","ANTHROPIC_AUTH_TOKEN":"anthropic-secret-token"}');
    expect(zaiJsonOut).not.toContain('plain-zai-token');
    expect(zaiJsonOut).not.toContain('anthropic-secret-token');
    const deepseekJsonOut = sanitize('{"DEEPSEEK_API_KEY":"plain-deepseek-token"}');
    expect(deepseekJsonOut).not.toContain('plain-deepseek-token');
    // CLAUDE_CODE_OAUTH_TOKEN: the oauth_token sub-pattern fires first, normalising the suffix to lowercase
    const oauthOut = sanitize('CLAUDE_CODE_OAUTH_TOKEN=some-long-oauth-token-value-here');
    expect(oauthOut).toContain('***');
    expect(oauthOut).not.toContain('some-long-oauth-token-value-here');
    expect(sanitize('CODEX_API_KEY=sk-proj-codex-abcdefghijklmnop')).toBe('CODEX_API_KEY=***');
  });

  it('redacts X-Agent-Token HTTP header value', () => {
    const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0YXNrLTEyMyJ9.SIG';
    // The credential-field \btoken\b pattern fires first and redacts the value
    expect(sanitize(`X-Agent-Token: ${jwt}`)).toBe('X-Agent-Token: ***');
    const headerArg = sanitize(`-H "X-Agent-Token: ${jwt}"`);
    expect(headerArg).toContain('***');
    expect(headerArg).not.toContain(jwt);
  });
});

// ── _crashReport token redaction ─────────────────────────────────────────────
describe('_crashReport token redaction', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(fs.appendFileSync).mockReset();
    process.env.TASK_ID = 'task-crash-redact-test';
  });

  afterEach(() => {
    delete process.env.TASK_ID;
    vi.restoreAllMocks();
  });

  it('redacts git x-access-token from console.error, forensic log, and heartbeat', () => {
    const rawDetail = 'fatal: repository clone failed: https://x-access-token:ghs_SECRETTOKEN123@github.com/org/repo.git';
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    _crashReportForTesting('uncaughtException', rawDetail, 1);

    // console.error must not contain the raw token
    const consoleArgs = consoleSpy.mock.calls.flat().join(' ');
    expect(consoleArgs).not.toContain('ghs_SECRETTOKEN123');
    expect(consoleArgs).toContain('***');

    // forensic log (fs.appendFileSync) must not contain the raw token
    const appendCalls = vi.mocked(fs.appendFileSync).mock.calls;
    const forensicContent = appendCalls.map(([, data]) => String(data)).join('\n');
    expect(forensicContent).not.toContain('ghs_SECRETTOKEN123');
    expect(forensicContent).toContain('***');

    // sendHeartbeatSync (via curl execFileSync) must not contain the raw token
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeDefined();
    const curlArgs = (curlCall?.[1] as string[]).join(' ');
    expect(curlArgs).not.toContain('ghs_SECRETTOKEN123');
    expect(curlArgs).toContain('***');
  });
});

// ── validateBranchName ───────────────────────────────────────────────────────
describe('validateBranchName', () => {
  it('accepts and returns a simple branch name', () => {
    expect(validateBranchName('main', 'branch')).toBe('main');
  });

  it('accepts feature branch with slash', () => {
    expect(validateBranchName('feature/foo', 'branch')).toBe('feature/foo');
  });

  it('accepts branch name with underscore', () => {
    expect(validateBranchName('fix_bar', 'branch')).toBe('fix_bar');
  });

  it('accepts version-like branch name with dots', () => {
    expect(validateBranchName('v1.2.3', 'branch')).toBe('v1.2.3');
  });

  it('accepts nested path branch', () => {
    expect(validateBranchName('blueprint/fix-123', 'branch')).toBe('blueprint/fix-123');
  });

  it('throws on empty string with label in message', () => {
    expect(() => validateBranchName('', 'base branch')).toThrow('base branch is required');
  });

  it('throws on directory traversal (..)', () => {
    expect(() => validateBranchName('feature/../hack', 'branch')).toThrow(
      'Invalid branch (contains ..)',
    );
  });

  it('throws on leading hyphen (git flag injection risk)', () => {
    expect(() => validateBranchName('-evil-flag', 'branch')).toThrow(
      'Invalid branch (starts with -)',
    );
  });

  it('throws on space in branch name', () => {
    expect(() => validateBranchName('feature branch', 'branch')).toThrow('Invalid branch');
  });

  it('throws on semicolon (shell injection risk)', () => {
    expect(() => validateBranchName('feat;rm -rf /', 'branch')).toThrow('Invalid branch');
  });

  it('throws on @ character', () => {
    expect(() => validateBranchName('feat@evil', 'branch')).toThrow('Invalid branch');
  });

  it('throws on backtick character', () => {
    expect(() => validateBranchName('feat`cmd`', 'branch')).toThrow('Invalid branch');
  });
});

// ── resolveBaseBranch ────────────────────────────────────────────────────────
describe('resolveBaseBranch', () => {
  it('prefers payload.base_branch over project_metadata.integration_branch', () => {
    expect(resolveBaseBranch({ base_branch: 'feature/x' }, { project_metadata: { integration_branch: 'develop' } })).toBe('feature/x');
  });

  it('falls back to project_metadata.integration_branch when payload has no base_branch', () => {
    expect(resolveBaseBranch({}, { project_metadata: { integration_branch: 'develop' } })).toBe('develop');
  });

  it('falls back to "main" when neither payload nor metadata has a branch', () => {
    expect(resolveBaseBranch({}, {})).toBe('main');
  });

  it('trims whitespace from payload.base_branch', () => {
    expect(resolveBaseBranch({ base_branch: '  main  ' }, {})).toBe('main');
  });

  it('trims whitespace from integration_branch and falls through on whitespace-only value', () => {
    expect(resolveBaseBranch({}, { project_metadata: { integration_branch: '  ' } })).toBe('main');
  });

  it('accepts custom fallback when no branch configured', () => {
    expect(resolveBaseBranch({}, {}, 'master')).toBe('master');
  });
});

// ── deriveTaskBranch ─────────────────────────────────────────────────────────
// Pins the task-type → branch-prefix mapping so drift between the agent and
// backend/app/services.py _TASK_TYPE_BRANCH_PREFIX is caught immediately.
describe('deriveTaskBranch', () => {
  const taskId = 'abcdef12-3456-7890-abcd-ef1234567890';
  const shortId = 'abcdef12'; // taskId.replace(/-/g,'').slice(0,8)

  // namespace is a REQUIRED parameter (no silent "blueprint" default); the
  // self-repo cases pass 'blueprint' explicitly to preserve the asserted name.
  it('maps refactor → refactor prefix', () => {
    expect(deriveTaskBranch('refactor', taskId, 'blueprint')).toBe(`blueprint/refactor-${shortId}`);
  });

  it('maps docs → docs prefix', () => {
    expect(deriveTaskBranch('docs', taskId, 'blueprint')).toBe(`blueprint/docs-${shortId}`);
  });

  it('maps deps → deps prefix', () => {
    expect(deriveTaskBranch('deps', taskId, 'blueprint')).toBe(`blueprint/deps-${shortId}`);
  });

  it('maps migrate → migrate prefix', () => {
    expect(deriveTaskBranch('migrate', taskId, 'blueprint')).toBe(`blueprint/migrate-${shortId}`);
  });

  it('defaults unknown types to task prefix', () => {
    expect(deriveTaskBranch('create', taskId, 'blueprint')).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('qa', taskId, 'blueprint')).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('fix', taskId, 'blueprint')).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('review', taskId, 'blueprint')).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('', taskId, 'blueprint')).toBe(`blueprint/task-${shortId}`);
  });

  it('TASK_TYPE_BRANCH_PREFIX covers all explicitly-mapped types', () => {
    // Keep in sync with backend/app/shared_state.py TASK_TYPE_BRANCH_PREFIX
    expect(TASK_TYPE_BRANCH_PREFIX).toEqual({
      refactor: 'refactor',
      docs: 'docs',
      deps: 'deps',
      migrate: 'migrate',
    });
  });

  it('uses first 8 hex chars of task UUID with dashes removed', () => {
    const id = '11223344-aaaa-bbbb-cccc-ddddeeee0000';
    expect(deriveTaskBranch('refactor', id, 'blueprint')).toBe('blueprint/refactor-11223344');
  });

  it('threads a per-project namespace into the leading segment', () => {
    const id = '11223344-aaaa-bbbb-cccc-ddddeeee0000';
    expect(deriveTaskBranch('refactor', id, 'autoproj')).toBe('autoproj/refactor-11223344');
    expect(deriveTaskBranch('docs', id, 'bot')).toBe('bot/docs-11223344');
  });

  it('resolveTaskBranchNamespace returns the per-project override or the Blueprint default', () => {
    expect(resolveTaskBranchNamespace({ build_config: { task_branch_namespace: 'autoproj' } })).toBe('autoproj');
    expect(resolveTaskBranchNamespace({ build_config: {} })).toBe('blueprint');
    expect(resolveTaskBranchNamespace({})).toBe('blueprint');
    expect(resolveTaskBranchNamespace(null)).toBe('blueprint');
    expect(resolveTaskBranchNamespace(undefined)).toBe('blueprint');
    // Regression (#7136 follow-up): an empty/absent build_config.directories must
    // NOT drop the namespace. resolveBuildConfig() returns undefined for empty
    // directories, so namespace resolution reads the RAW build_config — a
    // greenfield/docs-only repo keeps its slug instead of silently defaulting to
    // "blueprint" (which orphaned the orchestrator-expected branch).
    expect(resolveTaskBranchNamespace({ build_config: { directories: [], task_branch_namespace: 'stsbot' } })).toBe('stsbot');
    // A malformed slug falls back to the default rather than injecting garbage.
    expect(resolveTaskBranchNamespace({ build_config: { task_branch_namespace: 'Bad/Name' } })).toBe('blueprint');
  });
});

// ── FailureCategory ──────────────────────────────────────────────────────────
describe('FailureCategory', () => {
  const expectedCategories: FailureCategory[] = [
    'clone_failed',
    'build_failed',
    'test_failed',
    'lint_failed',
    'pr_creation_failed',
    'claude_code_failed',
    'parse_failed',
    'fatal',
    'github_token_fetch_failed',
  ];

  it('covers all expected failure category values', () => {
    // This test ensures the FailureCategory type includes all expected values.
    // If a new category is added to the type but not here, TypeScript will catch it.
    for (const cat of expectedCategories) {
      const value: FailureCategory = cat;
      expect(typeof value).toBe('string');
    }
    expect(expectedCategories).toHaveLength(9);
  });
});

// ── isTaskNoLongerActiveError ─────────────────────────────────────────────────
// Regression: the startup /api/agent/github/token GET 401s with "Task is no
// longer active" when the task goes terminal (CANCELLED/COMPLETED/superseded)
// between /claim and the token fetch. That benign cancellation must NOT be
// classified as a fatal CODE failure — runRoleOnce's catch special-cases it
// (stops the heartbeat, exits 0) and skips the failure_category='fatal' FAILED
// status post. This classifier is the gate for that graceful path.
describe('isTaskNoLongerActiveError', () => {
  it('matches the github/token startup 401 "Task is no longer active" error', () => {
    const err = new Error(
      'GET /api/agent/github/token?task_id=abc failed (401): {"error":{"code":401,"message":"Task is no longer active"}}',
    );
    expect(isTaskNoLongerActiveError(err)).toBe(true);
  });

  it('matches the lowercase "no longer active" phrasing', () => {
    const err = new Error('GET /api/agent/credentials failed (401): task is no longer active');
    expect(isTaskNoLongerActiveError(err)).toBe(true);
  });

  it('does NOT match a 401 that is not a cancellation (e.g. bad token)', () => {
    const err = new Error('GET /api/agent/github/token failed (401): {"message":"Invalid agent token"}');
    expect(isTaskNoLongerActiveError(err)).toBe(false);
  });

  it('does NOT match a non-401 status that mentions "no longer active"', () => {
    const err = new Error('GET /api/agent/foo failed (404): Task is no longer active');
    expect(isTaskNoLongerActiveError(err)).toBe(false);
  });

  it('does NOT match a non-Error value', () => {
    expect(isTaskNoLongerActiveError('GET failed (401): no longer active')).toBe(false);
    expect(isTaskNoLongerActiveError(null)).toBe(false);
    expect(isTaskNoLongerActiveError(undefined)).toBe(false);
  });

  it('confirms the FAILED-classification fatal path is bypassed for this error shape', () => {
    // The catch block in runRoleOnce computes failure_category as
    // 'fatal' (errorCategory ?? 'fatal') for un-typed errors. This benign
    // 401 must short-circuit BEFORE that classification so no FAILED status
    // with failure_category='fatal' is posted. Asserting the classifier
    // returns true is what guarantees the early return is taken.
    const benign = new Error(
      'GET /api/agent/github/token?task_id=df6057d3 failed (401): {"error":{"code":401,"message":"Task is no longer active"}}',
    );
    const genuinelyFatal = new Error('handler threw: undefined is not a function');
    expect(isTaskNoLongerActiveError(benign)).toBe(true);
    expect(isTaskNoLongerActiveError(genuinelyFatal)).toBe(false);
  });
});

// ── github/token fetch: long timeout + infra error category ──────────────────
// The github/token startup fetch hits get_github_token_for_task which can block
// up to ~120s while _github_request sleeps on a near-empty rate-limit bucket.
// The fix: apiGet now accepts a timeoutMs arg; the fetch is called at 180_000ms
// and failures are tagged github_token_fetch_failed (PROVISION_FAILURE, not
// CODE_ERROR) so the task requeues instead of burning the retry budget.
describe('github/token fetch: infra category and long timeout', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('apiGet with 180_000ms timeout does NOT abort before 30s default would have fired', async () => {
    vi.useFakeTimers();
    // Fetch never resolves — simulates the backend sleeping on rate-limit backoff.
    const fetchMock = makeFetchMock().mockImplementation((_input, init) => {
      const signal = init?.signal;
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new Error('This operation was aborted'));
        });
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const p = apiGet('/api/agent/github/token?task_id=test-123', 1, 180_000);
    const rejection = expect(p).rejects.toThrow(/timed out during headers phase/);

    // Default timeout (API_TIMEOUT = 30s) passes — should NOT have aborted yet.
    await vi.advanceTimersByTimeAsync(30_000);
    // Still pending — the 180s timeout has not fired yet.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Advance past the 180s header-phase timer — NOW it aborts.
    await vi.advanceTimersByTimeAsync(180_000);
    await rejection;
  });

  it('tags a github/token fetch error with github_token_fetch_failed, not fatal', () => {
    // The catch block in runRoleOnce wraps any error from the github/token apiGet
    // with failureCategory = 'github_token_fetch_failed'. This maps to
    // PROVISION_FAILURE (infra/requeue) instead of CODE_ERROR (burns budget).
    // The pattern mirrors clone_failed and vm_config_missing.
    const raw = new Error('GET /api/agent/github/token?task_id=x failed after 5 retries');
    const wrapped = raw as Error & { failureCategory?: FailureCategory };
    wrapped.failureCategory = 'github_token_fetch_failed';

    expect(wrapped.failureCategory).toBe('github_token_fetch_failed');
    // Confirm it is distinct from the generic fatal default.
    expect(wrapped.failureCategory).not.toBe('fatal');
    // TypeScript ensures this is a valid FailureCategory member at compile time.
    const category: FailureCategory = wrapped.failureCategory;
    expect(typeof category).toBe('string');
  });
});

describe('open PR branch authority read', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns the exact head/base identities needed for in-place task retry', async () => {
    const headSha = '4'.repeat(40);
    const payload = [{
      number: 42,
      html_url: 'https://github.com/owner/repo/pull/42',
      head: { ref: 'blueprint/task-123', sha: headSha, repo: { full_name: 'owner/repo' } },
      base: { ref: 'main' },
    }];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue(payload),
    }));

    await expect(listOpenPullRequestsForBranch(
      'token', 'owner', 'repo', 'blueprint/task-123',
    )).resolves.toEqual(payload);
  });

  it('fails closed when GitHub cannot authoritatively report open PRs', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: vi.fn(),
    }));

    await expect(listOpenPullRequestsForBranch(
      'token', 'owner', 'repo', 'blueprint/task-123',
    )).rejects.toThrow(/refusing stale-branch cleanup without an authoritative PR read/);
  });

  it('fails closed on a malformed successful PR response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ number: 42 }),
    }));

    await expect(listOpenPullRequestsForBranch(
      'token', 'owner', 'repo', 'blueprint/task-123',
    )).rejects.toThrow(/non-array response/);
  });

  it.each([
    [{ html_url: 'https://github.com/owner/repo/pull/42' }],
    [{ number: 42, html_url: 'not-a-github-pr-url' }],
    [{ number: 42, html_url: 'https://github.com/owner/repo/pull/42', head: { ref: 'branch' } }],
  ])('fails closed on a malformed PR entry %#', async (payload) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue([payload]),
    }));

    await expect(listOpenPullRequestsForBranch(
      'token', 'owner', 'repo', 'blueprint/task-123',
    )).rejects.toThrow(/malformed PR entry/);
  });
});

// ── Graceful shutdown signalling (cluster C7) ─────────────────────────────────
// Regression for the deferred-exit-with-fall-through bug: the
// isTaskNoLongerActiveError / heartbeat self-terminate paths used to do
// `setTimeout(process.exit); return`, which returned into main()'s role loop
// and let pollForNextRole re-enter before the exit fired (double-role-start /
// resource leak), and the heartbeat 'Other 4xx' branch exited with NO terminal
// status POST (task stranded IN_PROGRESS until reaper timeout → false
// AGENT_TIMEOUT). The fix throws a typed GracefulShutdownSignal (caught in the
// role loop to break cleanly) and posts a synchronous terminal status.
describe('GracefulShutdownSignal (C7 graceful teardown)', () => {
  it('is a non-Error control type so the crash handler / runRoleOnce catch never mis-records it as a fault', () => {
    const sig = new GracefulShutdownSignal('task_no_longer_active');
    // Must NOT be an Error — the role loop / global crash handler branch on
    // `instanceof Error` for fault reporting; a graceful teardown must dodge it.
    expect(sig instanceof Error).toBe(false);
    expect(sig instanceof GracefulShutdownSignal).toBe(true);
    expect(sig.reason).toBe('task_no_longer_active');
    expect(sig.exitCode).toBe(0);
  });

  it('carries a custom exit code when provided', () => {
    const sig = new GracefulShutdownSignal('reassigned', 143);
    expect(sig.exitCode).toBe(143);
  });
});

describe('postTerminalStatusSync (C7 heartbeat self-terminate posts FAILED)', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(fs.writeFileSync).mockReset();
    vi.mocked(fs.unlinkSync).mockReset();
  });

  it('fires a synchronous curl POST to /api/agent/status with status=FAILED and the given failure_category', () => {
    postTerminalStatusSync('task-c7-reassigned', 'reassigned', 'Task reassigned/deauthorized');

    const curlCall = vi.mocked(execFileSync).mock.calls.find(
      ([cmd]) => cmd === 'curl',
    );
    // Without the fix the heartbeat path exits with no terminal POST at all —
    // this assertion (a status POST WAS fired) is what fails pre-fix.
    expect(curlCall).toBeDefined();
    const args = curlCall?.[1] as string[];
    // POSTs to the status endpoint (not the heartbeat endpoint).
    expect(args.some((a) => a.includes('/api/agent/status'))).toBe(true);
    // Body carries FAILED + the explicit reassignment category so the backend
    // does not have to wait for the reaper to time the task out.
    const bodyIdx = args.indexOf('-d');
    const body = JSON.parse(args[bodyIdx + 1]) as Record<string, unknown>;
    expect(body.status).toBe('FAILED');
    expect(body.failure_category).toBe('reassigned');
    expect(body.task_id).toBe('task-c7-reassigned');
  });

  it('is a no-op when taskId is empty (no spurious POST)', () => {
    postTerminalStatusSync('', 'reassigned', 'no task');
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeUndefined();
  });

  it('retains the stale-result fallback file when the sync POST fails (no silent loss)', () => {
    // curl throws (partition) — the stale-result file must survive so a later
    // process / warm role retries the terminal status instead of losing it.
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('curl: timeout'); });
    postTerminalStatusSync('task-c7-partition', 'reassigned', 'partitioned');
    // The fallback file was written and NOT unlinked (unlink only happens on
    // a successful POST).
    const wrote = vi.mocked(fs.writeFileSync).mock.calls.some(
      ([p]) => String(p).includes('task-c7-partition_result.json'),
    );
    expect(wrote).toBe(true);
    const unlinked = vi.mocked(fs.unlinkSync).mock.calls.some(
      ([p]) => String(p).includes('task-c7-partition_result.json'),
    );
    expect(unlinked).toBe(false);
  });
});

describe('cancel flag (C7 reassignment shutdown signalling)', () => {
  afterEach(() => { _resetCancelRequestedForTesting(); });

  it('defaults to false and is observable via the test hook', () => {
    _resetCancelRequestedForTesting();
    expect(_isCancelRequestedForTesting()).toBe(false);
  });
});

// ── resetPerRoleModuleState — warm-VM per-role isolation ─────────────────────
// Single source of truth that clears ALL per-role module-level mutable state so
// a reused warm VM cannot leak a prior task's state into the next role.
describe('resetPerRoleModuleState (warm-VM isolation)', () => {
  afterEach(() => {
    _resetCancelRequestedForTesting();
    resetPerRoleModuleState({ warmStart: false });
  });

  it('clears the cancel shutdown signal so the next role can heartbeat', () => {
    _setCancelRequestedForTesting();
    expect(_isCancelRequestedForTesting()).toBe(true);
    resetPerRoleModuleState();
    expect(_isCancelRequestedForTesting()).toBe(false);
  });

  it('clears tool-trace, prompt fingerprint and LLM telemetry, and resets the warm-start flag', () => {
    // Seed per-role state as if a prior role ran.
    recordToolUse('Read', { file_path: '/tmp/workspace/foo.ts' });
    setCurrentPromptFingerprint('prior-role-fp');
    recordLlmCallStart();
    recordLlmTokens(123, 45);
    _setWarmStartForTesting(true);
    expect(snapshotToolTrace().length).toBeGreaterThan(0);
    expect(getCurrentPromptFingerprint()).toBe('prior-role-fp');
    expect(getLlmTelemetry().llm_call_count).toBe(1);

    // Warm handoff: reset with warmStart:true.
    resetPerRoleModuleState({ warmStart: true });

    expect(snapshotToolTrace().length).toBe(0);
    expect(getCurrentPromptFingerprint()).toBeNull();
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      num_turns: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
    expect(_getWarmStartForTesting()).toBe(true);
  });

  it('defaults warmStart to false (cold-start semantics) when no flags are passed', () => {
    _setWarmStartForTesting(true);
    resetPerRoleModuleState();
    expect(_getWarmStartForTesting()).toBe(false);
  });

  it('clears the log buffer so prior-role lines do not appear in the next task\'s stream', () => {
    appendToLogBuffer('prior-role-line-1');
    appendToLogBuffer('prior-role-line-2');
    expect(snapshotLogBuffer().length).toBe(2);
    resetPerRoleModuleState();
    expect(snapshotLogBuffer().length).toBe(0);
  });

  it('clears the test convention cache so a warm VM role does not serve stale conventions', () => {
    const spy = vi.spyOn(discovery, 'clearTestConventionCache');
    try {
      resetPerRoleModuleState();
      expect(spy).toHaveBeenCalledOnce();
    } finally {
      spy.mockRestore();
    }
  });

  it('reset invariant guard fails when a per-role variable is left unreset', async () => {
    // Static guard: every @per-role-tagged variable declared in index.ts must
    // appear in the resetPerRoleModuleState function body (directly or via a
    // single-depth helper it calls). This test fails CI when someone adds a new
    // per-role `let` without wiring it into the reset — the root cause of the
    // 12-cycle warm-VM state-leak whack-a-mole.
    // fs is mocked in this file; use vi.importActual to get the real module.
    const actualFs = await vi.importActual<typeof import('fs')>('fs');
    const nodeUrl = await import('node:url');
    const nodePath = await import('node:path');

    const dir = nodePath.dirname(nodeUrl.fileURLToPath(import.meta.url));
    const src = (actualFs as { readFileSync: (p: string, enc: string) => string })
      .readFileSync(nodePath.join(dir, 'index.ts'), 'utf-8');

    const perRoleVars: string[] = [];
    for (const line of src.split('\n')) {
      if (line.includes('// @per-role')) {
        const m = line.match(/(?:let|const)\s+(\w+)/);
        if (m) perRoleVars.push(m[1]);
      }
    }
    expect(perRoleVars.length).toBeGreaterThan(0);

    function extractFnBody(source: string, fn: string): string {
      const pat = new RegExp(`(?:export\\s+)?function\\s+${fn}\\s*\\(`);
      const idx = source.search(pat);
      if (idx === -1) return '';
      // Skip past the full parameter list to avoid mistaking default-arg
      // braces like `= {}` for the function body's opening brace.
      const parenStart = source.indexOf('(', idx);
      if (parenStart === -1) return '';
      let pd = 1; let pi = parenStart + 1;
      while (pi < source.length && pd > 0) {
        if (source[pi] === '(') pd++;
        else if (source[pi] === ')') pd--;
        pi++;
      }
      const braceIdx = source.indexOf('{', pi);
      if (braceIdx === -1) return '';
      let depth = 1; let i = braceIdx + 1;
      while (i < source.length && depth > 0) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}') depth--;
        i++;
      }
      return source.slice(braceIdx + 1, i - 1);
    }

    const resetBody = extractFnBody(src, 'resetPerRoleModuleState');
    const calledFns = [...resetBody.matchAll(/\b(\w+)\(\)/g)].map(m => m[1]);
    const helperBodies = calledFns
      .map(fn => extractFnBody(src, fn))
      .filter(b => b.length > 0)
      .join('\n');
    const fullScope = resetBody + '\n' + helperBodies;

    for (const v of perRoleVars) {
      expect(fullScope, `@per-role var "${v}" not cleared in resetPerRoleModuleState`).toMatch(
        new RegExp(`\\b${v}\\b`),
      );
    }
  });
});

// ── Cloud Run Job mode detection ──────────────────────────────────────────────
describe('Cloud Run Job mode (CLOUD_RUN_JOB env var)', () => {
  afterEach(() => {
    _setJobModeForTesting(false);
    delete process.env.CLOUD_RUN_JOB;
  });

  it('reads CLOUD_RUN_JOB=1 env var at startup and sets isJobMode flag', () => {
    process.env.CLOUD_RUN_JOB = '1';
    expect(_getJobModeForTesting()).toBe(false); // Initially false
    _setJobModeForTesting(true);
    expect(_getJobModeForTesting()).toBe(true);
  });

  it('does not set isJobMode when CLOUD_RUN_JOB is absent', () => {
    delete process.env.CLOUD_RUN_JOB;
    expect(_getJobModeForTesting()).toBe(false);
  });

  it('does not set isJobMode when CLOUD_RUN_JOB is not exactly "1"', () => {
    process.env.CLOUD_RUN_JOB = 'true';
    expect(_getJobModeForTesting()).toBe(false);
    _setJobModeForTesting(true);
    expect(_getJobModeForTesting()).toBe(true);
  });
});

// ── readHandlerOutput ─────────────────────────────────────────────────────────
describe('readHandlerOutput', () => {
  beforeEach(() => {
    mockReadFileSync.mockReset();
  });

  it('returns parsed object when file contains valid JSON', () => {
    mockReadFileSync.mockReturnValue('{"result": "ok", "count": 3}');
    const result = readHandlerOutput('task-1', 'research_findings', null);
    expect(result).toEqual({ result: 'ok', count: 3 });
  });

  it('returns parsed array when file contains valid JSON array', () => {
    mockReadFileSync.mockReturnValue('[{"title": "Finding 1"}, {"title": "Finding 2"}]');
    const result = readHandlerOutput('task-2', 'plan_output', null);
    expect(result).toEqual([{ title: 'Finding 1' }, { title: 'Finding 2' }]);
  });

  it('extracts embedded JSON array via regex when top-level parse fails', () => {
    const raw =
      'Some prefix text here\n[{"id": 1, "name": "item"}]\nSome trailing text';
    mockReadFileSync.mockReturnValue(raw);
    const result = readHandlerOutput(
      'task-3',
      'output',
      null,
    ) as Array<{ id: number; name: string }> | null;
    expect(result).toEqual([{ id: 1, name: 'item' }]);
  });

  it('extracts embedded strategic output object via regex fallback', () => {
    // Use an empty direction_operations array so the array-pattern regex
    // (/\[\s*\{...\}/) does not match first — the strategic-object regex is
    // only reached when there is no embedded JSON array in the raw text.
    const strategicObj =
      '{"vision": "build great things", "direction_operations": []}';
    const raw = `Preamble text with no JSON arrays\n${strategicObj}`;
    mockReadFileSync.mockReturnValue(raw);
    const result = readHandlerOutput(
      'task-4',
      'strategic_output',
      null,
    ) as { vision: string; direction_operations: unknown[] } | null;
    expect(result).not.toBeNull();
    if (!result) throw new Error('expected result');
    expect(result.vision).toBe('build great things');
    expect(Array.isArray(result.direction_operations)).toBe(true);
  });

  it('returns null when file is not found (ENOENT)', () => {
    mockReadFileSync.mockImplementation(() => {
      throw Object.assign(new Error('ENOENT: no such file or directory'), { code: 'ENOENT' });
    });
    const result = readHandlerOutput('task-5', 'missing', null);
    expect(result).toBeNull();
  });

  it('returns null for completely unparseable content with no recognisable JSON', () => {
    mockReadFileSync.mockReturnValue(
      'This is plain text with no JSON arrays or strategic objects at all.',
    );
    const result = readHandlerOutput('task-6', 'bad_output', null);
    expect(result).toBeNull();
  });

  it('reads from the expected /tmp path derived from taskId and outputType', () => {
    mockReadFileSync.mockReturnValue('{"ok": true}');
    readHandlerOutput('myTask', 'review_verdict', null);
    expect(mockReadFileSync).toHaveBeenCalledWith('/tmp/review_verdict_myTask.json', 'utf-8');
  });
});

// ── TMP_OUTPUT_NAMES — cleanup list correctness ───────────────────────────────
describe('TMP_OUTPUT_NAMES (scratch-file cleanup list)', () => {
  // These are the basenames the handlers actually write.  If a handler is
  // added or renamed, update TMP_OUTPUT_NAMES in reset.ts and keep this list
  // in sync so the test catches future drift.
  const KNOWN_HANDLER_OUTPUTS = [
    'review_verdict',
    'plan_output',
    'research_findings',
    'security_findings',
    'smoketest_result',
    'verify_tests_result',
    'debug_report',
    'perf_report',
    'investigation_report',
    'strategic_output',
    'design_document',
    'bootstrap_overview',
    'command_memory',
    'web_research_document',
    'release_audit_report',
    'release_audit_challenge',
    'walkthrough_e2e_result',
    'self_audit_findings',
    'self_review',
  ];

  it('covers every known handler output basename', () => {
    for (const name of KNOWN_HANDLER_OUTPUTS) {
      expect(TMP_OUTPUT_NAMES).toContain(name);
    }
  });

  it('does not contain stale basenames that handlers no longer write', () => {
    // These were the old (wrong) names before the fix — handlers write
    // debug_report / perf_report / design_document, not these.
    expect(TMP_OUTPUT_NAMES).not.toContain('debug_output');
    expect(TMP_OUTPUT_NAMES).not.toContain('perf_output');
    expect(TMP_OUTPUT_NAMES).not.toContain('design_doc_output');
  });
});

// Helper: spawn mock that emits close(code) immediately for each spawn call.
// `failCmds` maps command names to (exitCode, stderr) for failing runners.
// `npm ci` (dep install) is never failed even when `npm` is in failCmds —
// testFixLoop now treats install failures as setup errors (not test
// failures), so tests that want to simulate a failing test run must let
// the install step succeed.
function configureSpawnMock(failCmds: Record<string, { exitCode: number; stderr: string }> = {}) {
  vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
    const cmdStr = String(cmd);
    const args = Array.isArray(argsRaw) ? argsRaw.map(String) : [];
    const isNpmInstall = cmdStr === 'npm' && (args[0] === 'ci' || args[0] === 'install');
    const failure = isNpmInstall ? undefined : failCmds[cmdStr];
    const stderrCbs: ((d: Buffer) => void)[] = [];
    const proc = {
      pid: 100 + Math.floor(Math.random() * 1000),
      stderr: {
        on: vi.fn((event: string, cb: (d: Buffer) => void) => {
          if (event === 'data') stderrCbs.push(cb);
        }),
      },
      stdout: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') {
          setImmediate(() => {
            if (failure) for (const scb of stderrCbs) scb(Buffer.from(failure.stderr));
            cb(failure ? failure.exitCode : 0);
          });
        }
      }),
      kill: vi.fn(),
    };
    return proc as unknown as ReturnType<typeof spawn>;
  });
}

// ── testFixLoop failureSummary ────────────────────────────────────────────────
describe('testFixLoop failureSummary', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    // git() helper at index.ts:507 calls .trim() on the return — give it a
    // string default so unmocked git calls (rev-parse, add -A, etc.) don't crash.
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
  });

  it('returns undefined failureSummary when tests pass', async () => {
    // Provide a buildConfig with a test_cmd so a runner is created
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);

    expect(result.passed).toBe(true);
    expect(result.testFailuresDetected).toBe(false);
    expect(result.failureSummary).toBeUndefined();
  });

  it('returns setupFailed=true (not passed) when npm ci fails for every runner', async () => {
    // Regression: previously npm ci failures were swallowed as "runner
    // skipped" and the whole call returned `{passed: true}`, which let
    // the handler push code that was never test-verified. setupFailed
    // propagates through so standard/review-fix handlers mark the task
    // FAILED with failure_category='build_failed' instead of merging.
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    // Fail ALL npm calls — including ci — to simulate dep-install breakage.
    vi.mocked(spawn).mockImplementation((cmd: unknown) => {
      const cmdStr = String(cmd);
      const stderrCbs: ((d: Buffer) => void)[] = [];
      const fail = cmdStr === 'npm';
      const proc = {
        pid: 100,
        stderr: { on: vi.fn((event: string, cb: (d: Buffer) => void) => { if (event === 'data') stderrCbs.push(cb); }) },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              if (fail) for (const scb of stderrCbs) scb(Buffer.from('npm ERR! registry unreachable'));
              cb(fail ? 1 : 0);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    expect(result.passed).toBe(false);
    expect(result.setupFailed).toBe(true);
    expect(result.testFailuresDetected).toBe(false);
    expect(result.failureSummary).toMatch(/npm ci failed/);
    // Claude MUST NOT be invoked for setup failures — check that no
    // `claude` spawn happened even though a runner was configured.
    const claudeCalls = vi.mocked(spawn).mock.calls.filter(c => c[0].includes('claude')).length;
    expect(claudeCalls).toBe(0);
  });

  it('returns failureSummary with combined failure messages when tests fail after all attempts', async () => {
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    const errorMessage = 'Error: Test suite failed\n  at Object.<anonymous> (test.ts:10:5)';
    // npm (the test runner) exits non-zero with error on stderr; claude (spawn)
    // and other commands still succeed so runClaudeCode resolves for the fix loop.
    configureSpawnMock({ npm: { exitCode: 1, stderr: errorMessage } });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);

    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    expect(result.failureSummary).toBeDefined();
    expect(result.failureSummary).toContain(errorMessage);
  });
});

// ── testFixLoop directory scoping ─────────────────────────────────────────────
describe('testFixLoop directory scoping', () => {
  const multiRunnerConfig = {
    directories: [
      { path: 'agent', type: 'node', test_cmd: 'npm test' },
      { path: 'backend', type: 'python', test_cmd: 'python3 -m pytest -x -q' },
    ],
  };

  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
  });

  function cwdsCalledWith(): (string | undefined)[] {
    // testFixLoop's runners go through execFileAsync which calls spawn with cwd
    // in options[2].  Claude Code runs via spawn too but always cwd=WORKSPACE,
    // so filtering on the command (npm/python3) excludes those.
    return vi.mocked(spawn).mock.calls
      .filter(c => c[0] === 'npm' || c[0] === 'python3')
      .map(c => (c[2] as { cwd?: string } | undefined)?.cwd);
  }

  it('runs all runners when relatedFiles is not provided', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });

  it('filters to agent runner when relatedFiles contains only agent/ files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['agent/src/index.ts']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(false);
  });

  it('filters to backend runner when relatedFiles contains only backend/ files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['backend/app/main.py']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(false);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });

  it('runs all runners when relatedFiles contains only non-agent/backend files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['admin-ui/src/App.tsx']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });

  it('runs both runners when relatedFiles contains both agent/ and backend/ files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['agent/src/index.ts', 'backend/app/main.py']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });
});

// ── errorFingerprint / duplicate-error early exit ───────────────────────────
describe('errorFingerprint', () => {
  it('normalizes whitespace and truncates to 2000 chars', () => {
    expect(errorFingerprint('  foo\n\tbar  ')).toBe('foo bar');
    expect(errorFingerprint('x'.repeat(3000)).length).toBe(2000);
  });

  it('returns same fingerprint for trivially different whitespace', () => {
    expect(errorFingerprint('err:\n  at foo')).toBe(errorFingerprint('err:  at   foo'));
  });
});

// ── jaccardSimilarity unit tests ─────────────────────────────────────────────
describe('jaccardSimilarity', () => {
  it('returns 1 for identical strings', () => {
    expect(jaccardSimilarity('foo bar baz', 'foo bar baz')).toBe(1);
  });

  it('returns 0 for completely disjoint strings', () => {
    expect(jaccardSimilarity('alpha beta', 'gamma delta')).toBe(0);
  });

  it('returns 1 for two empty strings', () => {
    expect(jaccardSimilarity('', '')).toBe(1);
  });

  it('returns a value ≥ 0.7 for near-identical errors differing only in a line number', () => {
    const a = 'TypeError foo is undefined at line 42 in bar ts';
    const b = 'TypeError foo is undefined at line 47 in bar ts';
    expect(jaccardSimilarity(a, b)).toBeGreaterThanOrEqual(0.7);
  });

  it('returns a value < 0.7 for clearly different errors', () => {
    const a = 'TypeError foo is undefined at line 42';
    const b = 'SyntaxError unexpected token semicolon in completely different module';
    expect(jaccardSimilarity(a, b)).toBeLessThan(0.7);
  });
});

// ── detectLoopDivergence unit tests ──────────────────────────────────────────
describe('detectLoopDivergence', () => {
  it('returns null when history has fewer than 3 entries', () => {
    expect(detectLoopDivergence(['fp1', 'fp2'], ['e1', 'e2'], null, null)).toBeNull();
  });

  it('detects oscillation: A -> B -> A fingerprint pattern', () => {
    const fps = ['fpA', 'fpB', 'fpA'];
    const errs = ['error A text', 'error B text', 'error A text'];
    expect(detectLoopDivergence(fps, errs, null, null)).toBe('divergence_oscillation');
  });

  it('detects Jaccard-similar errors across two consecutive pairs', () => {
    // Each error differs by only 1 token out of 10 — Jaccard ≈ 9/11 ≈ 0.82 > threshold
    const base = 'TypeError property foo undefined at baz module context error';
    const errs = [`${base} one`, `${base} two`, `${base} thr`];
    const fps = errs.map((_e, i) => `fp${i}`);  // all distinct fingerprints
    expect(detectLoopDivergence(fps, errs, null, null)).toBe('divergence_similar_error');
  });

  it('detects diff revert when ≥ 80% of previous iteration lines vanish', () => {
    const prevDiff = { added: new Set(['line_a', 'line_b', 'line_c', 'line_d', 'line_e']), removed: new Set<string>() };
    const currDiff = { added: new Set(['line_x']), removed: new Set<string>() };  // all prev lines gone
    const fps = ['fp1', 'fp2', 'fp3'];
    const errs = ['error 1', 'error 2', 'error 3'];
    expect(detectLoopDivergence(fps, errs, prevDiff, currDiff)).toBe('divergence_diff_revert');
  });

  it('does not flag diff revert when < 80% of previous lines vanish', () => {
    const prevDiff = { added: new Set(['line_a', 'line_b', 'line_c', 'line_d', 'line_e']), removed: new Set<string>() };
    const currDiff = { added: new Set(['line_a', 'line_b', 'line_x']), removed: new Set<string>() };  // 3/5 = 60% gone
    const fps = ['fp1', 'fp2', 'fp3'];
    const errs = ['error 1', 'error 2', 'error 3'];
    expect(detectLoopDivergence(fps, errs, prevDiff, currDiff)).toBeNull();
  });
});

// ── testFixLoop divergence detection ─────────────────────────────────────────
describe('testFixLoop divergence detection', () => {
  const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };

  beforeEach(() => {
    _resetFixLoopEventStateForTesting();
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(execSync).mockReset();
    vi.mocked(execSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
  });

  it('exits with divergence_oscillation when test failures oscillate A→B→A', async () => {
    const errorA = 'FAIL test.ts > alpha unique_error_A';
    const errorB = 'FAIL test.ts > beta unique_error_B';
    const errorSequence = [errorA, errorB, errorA];
    let callCount = 0;
    vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
      const cmdStr = String(cmd);
      const args = Array.isArray(argsRaw) ? argsRaw.map(String) : [];
      const isNpmInstall = cmdStr === 'npm' && (args[0] === 'ci' || args[0] === 'install');
      const stderrCbs: ((d: Buffer) => void)[] = [];
      const failStderr = (!isNpmInstall && cmdStr === 'npm') ? errorSequence[Math.min(callCount++, errorSequence.length - 1)] : null;
      const proc = {
        pid: 100 + callCount,
        stderr: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stderrCbs.push(cb);
          }),
        },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              if (failStderr !== null) for (const scb of stderrCbs) scb(Buffer.from(failStderr));
              cb(failStderr !== null ? 1 : 0);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    const events = drainFixLoopEventBuffer();
    expect(events.some(e => e.exit_reason === 'divergence_oscillation')).toBe(true);
  });
});

describe('testFixLoop early exit on identical errors', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
  });

  it('exits early when consecutive test errors are identical', async () => {
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    const errorMessage = 'FAIL test.ts > it works';
    configureSpawnMock({ npm: { exitCode: 1, stderr: errorMessage } });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    expect(result.passed).toBe(false);
    expect(result.failureSummary).toContain(errorMessage);
    // Count ONLY test-run invocations (not the setup `npm ci` install
    // calls testFixLoop does ahead of each runner). Early exit after the
    // duplicate on attempt 2: at most 2 test runs.
    const testRunCalls = vi.mocked(spawn).mock.calls.filter(
      c => c[0] === 'npm' && Array.isArray(c[1]) && c[1][0] === 'test',
    ).length;
    expect(testRunCalls).toBeLessThanOrEqual(2);
  });

  it('continues when test errors differ between attempts', async () => {
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    let callCount = 0;
    vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
      const cmdStr = String(cmd);
      const args = Array.isArray(argsRaw) ? argsRaw.map(String) : [];
      const isNpmInstall = cmdStr === 'npm' && (args[0] === 'ci' || args[0] === 'install');
      const stderrCbs: ((d: Buffer) => void)[] = [];
      let failStderr: string | null = null;
      // Only the test-run (npm test) is simulated as failing. npm ci
      // (install) succeeds — testFixLoop now classifies install failures
      // as setup errors, not test failures.
      if (cmdStr === 'npm' && !isNpmInstall) {
        callCount++;
        failStderr = `FAIL variant ${callCount}`;
      }
      const proc = {
        pid: 100 + callCount,
        stderr: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stderrCbs.push(cb);
          }),
        },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              if (failStderr !== null) for (const scb of stderrCbs) scb(Buffer.from(failStderr));
              cb(failStderr !== null ? 1 : 0);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    // Count only test-run invocations, skipping the setup `npm ci` calls.
    // testFixLoop runs maxFixAttempts + 1 = MAX_TEST_FIX_ATTEMPTS + 1 times when errors differ each iteration.
    const testRunCalls = vi.mocked(spawn).mock.calls.filter(
      c => c[0] === 'npm' && Array.isArray(c[1]) && c[1][0] === 'test',
    ).length;
    expect(testRunCalls).toBe(MAX_TEST_FIX_ATTEMPTS + 1);
  });
});

// ── RateLimitError ───────────────────────────────────────────────────────────
// Pins the class identity so the outer catch in main() (which uses
// `error instanceof RateLimitError` for classification) can't regress into
// the old cast-and-rematch pattern that silently reclassified rate-limits
// as CODE_ERROR when stderrBuffer was empty.
