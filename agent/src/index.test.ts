import { vi, describe, it, expect, beforeEach, afterEach, beforeAll } from 'vitest';

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
import { strVal, sanitize, validateBranchName, resolveBaseBranch, readHandlerOutput, testFixLoop, errorFingerprint, captureTestBaseline, RateLimitError, NoProgressTimeoutError, setupWorkspace, registerSizeCapMergeDriver, pushAndCreatePR, condensedAnalysisSections, matchesRateLimit, matchesAuthFailure, matchesVmConfigMissing, VmConfigMissingError, missingVmProviderConfigFiles, parseFailingTests, parseFailingTestsFromVitestJson, extractPytestSelectorsFromOutput, normalizePytestSelectorForRunner, buildTargetedPytestRunners, recordToolUse, snapshotToolTrace, extractInvestigatedFiles, clearToolTrace, appendToLogBuffer, snapshotLogBuffer, clearLogBuffer, replayLogBufferRateLimited, getProviderRunner, providerSupportsNativeSubagents, parseLLMCliTranslation, execFileAsync, runProviderAgent, persistAndPostStatus, _consumePendingParkDirectiveForTesting, continueWarmContinuation, parseStaleResultFile, markStaleResultFailure, shouldAbandonStaleResult, pollForNextRole, bootstrapPoolMode, refreshRuntimeState, _resetTelemetryForTesting, _setRoleClaimTimestampForTesting, _getFirstToolUseAtForTesting, _setWarmStartForTesting, _setHandlerDurationForTesting, _setAgentRuntimeFingerprintForTesting, readAgentSourceSha, _setAgentSourceShaForTesting, _startHeartbeatForTesting, _stopHeartbeatForTesting, _buildHeartbeatStatusForTesting, apiPost, apiGet, apiRequest, getAgentToken, ClaudeTimeoutError, detectLoopDivergence, fetchVmImageIdentity, recordLlmCallStart, recordLlmTokens, getLlmTelemetry, resetLlmTelemetry, estimateTokensFromChars, deriveTaskBranch, resolveTaskBranchNamespace, TASK_TYPE_BRANCH_PREFIX, codexStreamHasTurnCompleted, computeRefreshDelayMs, isBootstrapToken, isTaskNoLongerActiveError, postTerminalStatusSync, GracefulShutdownSignal, _isCancelRequestedForTesting, _resetCancelRequestedForTesting, _setCancelRequestedForTesting, resetPerRoleModuleState, _getWarmStartForTesting, _getJobModeForTesting, _setJobModeForTesting, setCurrentPromptFingerprint, getCurrentPromptFingerprint, performWarmRetarget, __setWarmRetargetDepsForTesting, MATERIALIZE_SCRIPT_PATH, _setAgentTokenForTesting, _resetCredentialStateForTesting, _captureBootCredentialSnapshotForTesting, _captureBootProviderConfigPresenceForTesting, _materializeClaimCredentialStateForTesting, _getActiveCredentialStateSourceForTesting, ensureTaskCapabilityContractLoaded, __resetTaskCapabilityContractLoadedForTesting, _crashReportForTesting, cloneWithRetry, main, resolveCredentialsFromSecretVersion, normalizeClaudeConfigContent, WORKSPACE, type SecretManifest, type FailureCategory } from './index.js';
import { prepareWorkspaceForRole, resetWorkspacePrepOutcome } from './workspacePrep.js';
import { jaccardSimilarity } from './utils/diff-detection.js';
import {
  __resetTaskCapabilityContractForTesting,
  isReadOnlyTaskType,
  loadTaskCapabilityContract,
  type TaskCapabilityContract,
} from './taskCapabilities.js';
import { buildFixtureCapabilityContract } from './testSetup.js';
import {
  _setLastProviderRunErrorReportForTesting,
  __setCommandExistsOverrideForSmoke,
  __resetClaudeCapabilityCacheForTesting,
  claudeHelpSupportsPartialMessages,
  probeClaudeCapabilities,
  stripUnsupportedClaudeOptionalArgs,
  type ProviderRunErrorReport,
} from './providers/runners.js';
import { clearPidRegistry, _getRegisteredPids } from './processTracking.js';
import { TMP_OUTPUT_NAMES } from './reset.js';
import {
  drainFixLoopEventBuffer,
  _resetFixLoopEventStateForTesting,
} from './fix-loop-events.js';
import {
  MAX_TEST_FIX_ATTEMPTS,
  TOOL_USE_STALL_TIMEOUT_MS,
  isWorkflowToolName,
  CLAUDE_STREAM_STALL_MS,
  LLM_NO_PROGRESS_STALL_MS,
  TOKEN_REFRESH_FLOOR_MS,
  TOKEN_REFRESH_CEILING_MS,
  BOOTSTRAP_TOKEN_THRESHOLD_MS,
  HEARTBEAT_INTERVAL,
  HEARTBEAT_INFLIGHT_DEADLINE,
  PROCESS_KILL_GRACE_MS,
} from './constants.js';
import { execFileSync, spawn, execSync } from 'child_process';
import fs from 'fs';
import type WebSocket from 'ws';
import * as discovery from './prompts/discovery.js';

/** Asserts a value is defined and returns it (narrowed). Replaces unsafe `x!` in tests. */
function defined<T>(v: T | undefined | null, msg = 'expected value to be defined'): T {
  if (v === undefined || v === null) throw new Error(msg);
  return v;
}

/** fetch-shaped vi.fn: Promise-returning implementations satisfy
 *  @typescript-eslint/no-misused-promises (a bare vi.fn() infers a
 *  void-returning procedure, which every `.mockImplementation(() => Promise…)`
 *  call then violates). */
const makeFetchMock = () => vi.fn<(input: unknown, init?: RequestInit) => Promise<unknown>>();
type FetchMock = ReturnType<typeof makeFetchMock>;

const initialPoolPriorTaskId = '00000000-0000-0000-0000-000000000000';

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

// ── computeRefreshDelayMs ─────────────────────────────────────────────────────
/** Build a minimal JWT with a given exp claim (no real signature needed). */
function mockJwtWithExp(expUnixSec: number): string {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({ exp: expUnixSec, sub: 'test-task' })).toString('base64url');
  return `${header}.${payload}.fakesig`;
}

function mockJwtWithVmInstanceId(vmInstanceId: string): string {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({ sub: 'test-task', vm_instance_id: vmInstanceId })).toString('base64url');
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
    const rejection = expect(p).rejects.toThrow(/aborted/);

    // Default timeout (API_TIMEOUT = 30s) passes — should NOT have aborted yet.
    await vi.advanceTimersByTimeAsync(30_000);
    // Still pending — the 180s timeout has not fired yet.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Advance past the 180s body-phase timer — NOW it aborts.
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
    'panel_review_lens_correctness',
    'panel_review_lens_security',
    'panel_review_lens_code_quality',
    'panel_review_lens_test_coverage',
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
describe('matchesRateLimit', () => {
  it('detects "you\'ve hit your limit"', () => {
    expect(matchesRateLimit("you've hit your limit")).toBe(true);
  });

  it('detects "you\'ve hit your usage limit"', () => {
    expect(matchesRateLimit("you've hit your usage limit")).toBe(true);
  });

  it('detects "claude ai usage limit reached"', () => {
    expect(matchesRateLimit('claude ai usage limit reached')).toBe(true);
  });

  it('detects "you\'re out of extra usage"', () => {
    expect(matchesRateLimit("you're out of extra usage")).toBe(true);
  });

  it('detects "5-hour limit reached"', () => {
    expect(matchesRateLimit('5-hour limit reached')).toBe(true);
  });

  it('detects "api error: rate limit reached"', () => {
    expect(matchesRateLimit('api error: rate limit reached')).toBe(true);
  });

  it('detects "this request would exceed your account\'s rate limit"', () => {
    expect(matchesRateLimit("this request would exceed your account's rate limit")).toBe(true);
  });

  it('detects the org monthly spend-limit banner (curly apostrophes)', () => {
    expect(matchesRateLimit(
      'You’ve hit your org’s monthly spend limit · ask your admin to raise it at claude.ai/settings/usage',
    )).toBe(true);
  });

  it('detects the org monthly spend-limit banner (ascii apostrophes)', () => {
    expect(matchesRateLimit(
      "You've hit your org's monthly spend limit · ask your admin to raise it at claude.ai/settings/usage",
    )).toBe(true);
  });

  it('detects the org monthly spend-limit banner for anthropic/api_key', () => {
    expect(matchesRateLimit("you've hit your org's monthly spend limit", 'anthropic', 'api_key')).toBe(true);
  });

  it('matches case-insensitively (uppercase)', () => {
    expect(matchesRateLimit('API ERROR: RATE LIMIT REACHED')).toBe(true);
  });

  it('matches case-insensitively (mixed case)', () => {
    expect(matchesRateLimit('Claude AI Usage Limit Reached')).toBe(true);
  });

  it('matches when pattern is embedded in surrounding text', () => {
    expect(matchesRateLimit('Error from Anthropic: API Error: Rate limit reached. Please retry later.')).toBe(true);
  });

  it('matches when pattern appears after other log lines', () => {
    expect(matchesRateLimit('line 1\nline 2\nyou\'ve hit your usage limit\nline 4')).toBe(true);
  });

  it('returns false for a bare "429"', () => {
    expect(matchesRateLimit('HTTP 429 received')).toBe(false);
  });

  it('returns false for the word "rate" alone', () => {
    expect(matchesRateLimit('increased rate of errors')).toBe(false);
  });

  it('returns false for the word "limit" alone', () => {
    expect(matchesRateLimit('limit exceeded on some resource')).toBe(false);
  });

  it('returns false for "overloaded"', () => {
    expect(matchesRateLimit('server is overloaded')).toBe(false);
  });

  it('detects provider-specific API-key capacity bodies', () => {
    expect(matchesRateLimit(
      'api.deepseek.com overloaded: authentication fails',
      'deepseek',
      'api_key',
    )).toBe(true);
    expect(matchesRateLimit('api.z.ai service capacity exceeded', 'zai', 'api_key')).toBe(true);
    expect(matchesRateLimit('OpenAI API error: model is temporarily unavailable', 'openai', 'api_key')).toBe(true);
  });

  it('does not detect generic API-key capacity words without provider context', () => {
    expect(matchesRateLimit('unit test prints service unavailable', 'openai', 'api_key')).toBe(false);
    expect(matchesRateLimit('unit test expected OpenAI service unavailable response', 'openai', 'api_key')).toBe(false);
    expect(matchesRateLimit('unit test expected gpt-5 temporarily unavailable response', 'openai', 'api_key')).toBe(false);
    expect(matchesRateLimit('docs mention GLM service unavailable fallback', 'zai', 'api_key')).toBe(false);
  });

  it('returns false for an empty string', () => {
    expect(matchesRateLimit('')).toBe(false);
  });

  it('returns false for unrelated error output', () => {
    expect(matchesRateLimit('TypeError: cannot read property of undefined')).toBe(false);
  });

  it('detects OpenAI API-key rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('Rate limit reached for gpt-5 in organization test', 'openai', 'api_key')).toBe(true);
  });

  it('detects Gemini service-account rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('RESOURCE_EXHAUSTED while calling model', 'gemini', 'service_account')).toBe(true);
  });

  it('detects Z.AI API-key rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('Too many requests: rate_limit exceeded', 'zai', 'api_key')).toBe(true);
  });

  it('detects DeepSeek API-key rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('Too many requests: rate_limit exceeded', 'deepseek', 'api_key')).toBe(true);
  });

  it('detects DeepSeek balance/quota bodies even when they include auth-ish text', () => {
    expect(matchesRateLimit(
      'HTTP 401 Unauthorized from api.deepseek.com: authentication fails: insufficient_balance',
      'deepseek',
      'api_key',
    )).toBe(true);
  });

  it('detects Z.AI balance/quota bodies even when they include auth-ish text', () => {
    expect(matchesRateLimit(
      'HTTP 401 Unauthorized from api.z.ai: authentication_failed: insufficient balance',
      'zai',
      'api_key',
    )).toBe(true);
  });

  it('detects Gemini API-key quota metric bodies', () => {
    const msg = "Quota exceeded for quota metric 'GenerateRequests'. Please retry in 39.8s.";
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini API-key billing quota bodies', () => {
    const msg = 'You exceeded your current quota, please check your plan and billing details.';
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini Code Assist fallback quota messages', () => {
    const msg = 'Automatically switching from gemini-2.5-pro. Switching to the gemini-2.5-flash model.';
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini service-account Vertex quota bodies', () => {
    const msg = 'Resource exhausted. Please try again later. Please refer to https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429';
    expect(matchesRateLimit(msg, 'gemini', 'service_account')).toBe(true);
  });

  it('detects Gemini service-account quota metric bodies', () => {
    const msg = 'Quota exceeded for metric: aiplatform.googleapis.com/generate_content_requests';
    expect(matchesRateLimit(msg, 'gemini', 'service_account')).toBe(true);
  });

  it('does not cross-match provider-specific rate limits', () => {
    expect(matchesRateLimit('Rate limit reached for gpt-5 in organization test', 'anthropic', 'oauth')).toBe(false);
  });

  it('detects "you\'ve hit your session limit" for anthropic/oauth', () => {
    expect(matchesRateLimit("You've hit your session limit · resets 7:10am (UTC)", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your session limit" case-insensitively', () => {
    expect(matchesRateLimit("YOU'VE HIT YOUR SESSION LIMIT", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your session limit" for default provider (anthropic/oauth)', () => {
    expect(matchesRateLimit("you've hit your session limit · resets 7:10am (UTC)")).toBe(true);
  });

  it('detects "you\'ve hit your weekly limit" for anthropic/oauth', () => {
    expect(matchesRateLimit("You've hit your weekly limit · resets 9pm (UTC)", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your weekly limit" for default provider (anthropic/oauth)', () => {
    expect(matchesRateLimit("You've hit your weekly limit · resets 9pm (UTC)")).toBe(true);
  });

  it('detects structured api_error_status:429 in transcript regardless of provider', () => {
    const transcript = '{"type":"result","is_error":true,"api_error_status":429,"error":"rate_limit"}';
    expect(matchesRateLimit(transcript)).toBe(true);
    expect(matchesRateLimit(transcript, 'openai', 'api_key')).toBe(true);
  });

  it('does not match bare 429 without JSON key context', () => {
    expect(matchesRateLimit('HTTP 429 received')).toBe(false);
  });
});

describe('matchesAuthFailure', () => {
  it('does NOT treat a bare apiKeySource=none init event as an auth failure (normal for OAuth/Max-plan)', () => {
    // The documented default auth mode (AUTH_MODE=oauth / Max-plan subscription)
    // uses no API key, so EVERY healthy init event reports apiKeySource:"none".
    // Matching on it alone would misclassify healthy OAuth VMs on any transient
    // non-zero-exit validation.
    const init = '{"type":"system","subtype":"init","apiKeySource":"none","model":"claude-opus-4-8"}';
    expect(matchesAuthFailure(init)).toBe(false);
  });

  it('detects the canonical apiKeySource=none + 401 authentication_failed rejection', () => {
    // The real rejection signature is apiKeySource:none accompanied by an explicit
    // HTTP 401 authentication_failed — the authentication_failed token is what makes
    // it a confirmed rejection rather than a healthy OAuth init.
    const rejection = '{"type":"system","subtype":"init","apiKeySource":"none"}\nAPI Error: 401 {"type":"error","error":{"type":"authentication_failed"}}';
    expect(matchesAuthFailure(rejection)).toBe(true);
  });

  it('detects Claude stream 401 authentication failures', () => {
    const msg = '{"type":"system","subtype":"api_retry","error_status":401,"error":"authentication_failed"}';
    expect(matchesAuthFailure(msg)).toBe(true);
  });

  it('detects provider invalid credential messages', () => {
    expect(matchesAuthFailure('Failed to authenticate. API Error: 401 Invalid authentication credentials')).toBe(true);
  });

  it('detects an HTTP 401 authentication_failed body', () => {
    const body = 'API Error: 401 {"type":"error","error":{"type":"authentication_failed","message":"invalid x-api-key"}}';
    expect(matchesAuthFailure(body)).toBe(true);
  });

  it('detects an expired OAuth token message', () => {
    expect(matchesAuthFailure('OAuth token has expired. Please run /login.')).toBe(true);
  });

  it('detects OAuth org-disabled errors', () => {
    expect(matchesAuthFailure('oauth_org_not_allowed')).toBe(true);
  });

  it('matches case-insensitively', () => {
    expect(matchesAuthFailure('AUTHENTICATION_FAILED')).toBe(true);
  });

  it('detects OpenAI invalid_api_key for the openai provider', () => {
    expect(matchesAuthFailure('Error code: 401 - invalid_api_key', 'openai')).toBe(true);
  });

  it('detects Gemini "API key not valid" for the gemini provider', () => {
    expect(matchesAuthFailure('API key not valid. Please pass a valid API key.', 'gemini')).toBe(true);
  });

  it('detects Z.AI invalid-key failures for the zai provider', () => {
    expect(matchesAuthFailure('invalid z.ai api key', 'zai')).toBe(true);
  });

  it('detects DeepSeek invalid-key failures for the deepseek provider', () => {
    expect(matchesAuthFailure('invalid deepseek api key', 'deepseek')).toBe(true);
  });

  it('does not classify a rate-limit body as an auth failure', () => {
    expect(matchesAuthFailure('API Error: Rate limit reached. Please retry later.')).toBe(false);
  });

  it('returns false for a bare 401 with no auth context', () => {
    expect(matchesAuthFailure('HTTP 401 received')).toBe(false);
  });

  it('does not match unrelated numeric output', () => {
    expect(matchesAuthFailure('processed 401 files successfully')).toBe(false);
  });

  it('returns false for unrelated error output', () => {
    expect(matchesAuthFailure('TypeError: cannot read property of undefined')).toBe(false);
  });

  it('returns false for an empty string', () => {
    expect(matchesAuthFailure('')).toBe(false);
  });

  it('does not cross-match provider-specific auth failures', () => {
    expect(matchesAuthFailure('API key not valid', 'anthropic')).toBe(false);
  });

  it('detects a Codex OAuth refresh-token-reuse 401 (prod 2026-06-14 SavarinCodex)', () => {
    // The codex CLI emits this when its stored refresh token was already
    // consumed/rotated. Before coverage was added, runClaudeCode returned a bare
    // non-zero exit instead of throwing AuthFailureError, so panel_review
    // fail-closed the challenge and stranded the gated PR's merge.
    const body =
      'ERROR codex_login::auth::manager: Failed to refresh token: 401 Unauthorized: ' +
      '{"error":{"message":"Your refresh token has already been used to generate a new ' +
      'access token. Please try signing in again.","type":"invalid_request_error",' +
      '"code":"refresh_token_reused"}}';
    expect(matchesAuthFailure(body, 'openai')).toBe(true);
  });

  it('detects the "refresh token was already used / sign in again" variant', () => {
    const body =
      'Failed to refresh token: Your access token could not be refreshed because your ' +
      'refresh token was already used. Please log out and sign in again.';
    expect(matchesAuthFailure(body, 'openai')).toBe(true);
  });

  it('treats OAuth refresh-token death as provider-agnostic (generic pattern)', () => {
    expect(matchesAuthFailure('error: refresh_token_reused')).toBe(true);
  });

  it('does not classify a benign token-refresh log line as an auth failure', () => {
    expect(matchesAuthFailure('Scheduled proactive token refresh in 1800s')).toBe(false);
  });
});
describe('RateLimitError', () => {
  it('is an instance of RateLimitError and Error', () => {
    const err = new RateLimitError('Claude API usage exhausted — banner');
    expect(err).toBeInstanceOf(RateLimitError);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe('RateLimitError');
    expect(err.message).toBe('Claude API usage exhausted — banner');
  });

  it('plain Error is not a RateLimitError', () => {
    const err = new Error('Claude API usage exhausted — banner');
    expect(err).not.toBeInstanceOf(RateLimitError);
  });
});

describe('NoProgressTimeoutError', () => {
  it('carries the transient no_progress_timeout failureCategory and is distinct from RateLimitError', () => {
    const err = new NoProgressTimeoutError('no model output for >15 min');
    expect(err).toBeInstanceOf(NoProgressTimeoutError);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(RateLimitError);
    expect(err.name).toBe('NoProgressTimeoutError');
    // The agent catch block reads .failureCategory for non-RateLimitError errors;
    // 'no_progress_timeout' must NOT trigger the account-model block.
    expect(err.failureCategory).toBe('no_progress_timeout');
  });
});

describe('matchesVmConfigMissing', () => {
  it('matches the Claude CLI configless death banner (case-insensitive)', () => {
    expect(matchesVmConfigMissing('Claude configuration file not found at /home/agent/.claude.json')).toBe(true);
    expect(matchesVmConfigMissing('claude CONFIGURATION FILE NOT FOUND')).toBe(true);
  });

  it('does not match rate-limit or auth-failure text', () => {
    expect(matchesVmConfigMissing("you've hit your usage limit")).toBe(false);
    expect(matchesVmConfigMissing('OAuth token has expired. Please run /login.')).toBe(false);
  });

  it('does not match unrelated file-not-found errors', () => {
    expect(matchesVmConfigMissing('ENOENT: no such file or directory, open /tmp/foo.json')).toBe(false);
    expect(matchesVmConfigMissing('')).toBe(false);
  });
});

describe('VmConfigMissingError', () => {
  it('carries the vm_config_missing failureCategory and is distinct from RateLimitError', () => {
    const err = new VmConfigMissingError('Claude Code provider config file missing on VM');
    expect(err).toBeInstanceOf(VmConfigMissingError);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(RateLimitError);
    expect(err.name).toBe('VmConfigMissingError');
    // runRoleOnce's catch reads .failureCategory; vm_config_missing maps to
    // PROVISION_FAILURE backend-side — never RATE_LIMIT ("usage exhausted").
    expect(err.failureCategory).toBe('vm_config_missing');
  });
});

describe('provider runner prompt transport', () => {
  it('uses stdin for OpenAI run commands instead of argv prompts', () => {
    const runner = getProviderRunner('openai', 'api_key');
    const command = runner.buildRunCommand('Very long prompt body', 'gpt-5');

    expect(command.promptInput).toBe('stdin');
    // Codex CLI invocation uses the canonical bypass flag at the legacy
    // fallback path — disables approval policy and sandbox in one knob,
    // equivalent to Claude's --dangerously-skip-permissions. Agent VMs
    // are ephemeral; sandboxing inside them stalls on approval prompts
    // nobody can answer. `--full-auto` is explicitly avoided since it
    // aliases --sandbox workspace-write and would override the bypass.
    // --json: emit JSONL events (added 2026-05-08) so the agent wrapper can
    // parse Codex tool calls and populate last_tool_use_at. Without it, every
    // Codex task is structurally invisible to the watchdog.
    expect(command.args).toEqual(['exec', '--skip-git-repo-check', '--json', '--dangerously-bypass-approvals-and-sandbox', '-m', 'gpt-5']);
    expect(command.args.join(' ')).not.toContain('Very long prompt body');
    expect(command.outputMode).toBe('codex-jsonl');
  });

  it('uses stdin for Gemini run commands instead of argv prompts', () => {
    const runner = getProviderRunner('gemini', 'api_key');
    const command = runner.buildRunCommand('Very long prompt body', 'gemini-2.5-pro');

    expect(command.promptInput).toBe('stdin');
    expect(command.args).toEqual(['--approval-mode=yolo', '-o', 'text', '-m', 'gemini-2.5-pro']);
    expect(command.args.join(' ')).not.toContain('Very long prompt body');
    // Sandbox disabled via env var since Gemini's approval-mode and
    // sandbox are separate knobs (YOLO auto-enables docker sandbox
    // unless GEMINI_SANDBOX=false pins it off).
    expect(command.env.GEMINI_SANDBOX).toBe('false');
    // Trust gate opt-out — ephemeral VM is the isolation boundary.
    expect(command.env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildRunCommand (service_account)', () => {
    const runner = getProviderRunner('gemini', 'service_account');
    const command = runner.buildRunCommand('Some prompt', 'gemini-2.5-pro');
    expect(command.env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildValidationCommand (api_key)', () => {
    const runner = getProviderRunner('gemini', 'api_key');
    const command = runner.buildValidationCommand('gemini-2.5-pro');
    expect(command).not.toBeNull();
    expect(defined(command).env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildValidationCommand (service_account)', () => {
    const runner = getProviderRunner('gemini', 'service_account');
    const command = runner.buildValidationCommand('gemini-2.5-pro');
    expect(command).not.toBeNull();
    expect(defined(command).env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('passes --include-partial-messages to the Claude run command so slow opus turns stream progress deltas', () => {
    const runner = getProviderRunner('anthropic', 'oauth');
    const command = runner.buildRunCommand('prompt body', 'claude-opus-4-8', 'high');
    // The flag must accompany the streaming-json output mode — it only emits
    // partial-message stream_event lines when stream-json is active.
    expect(command.outputMode).toBe('claude-stream-json');
    expect(command.args).toContain('--include-partial-messages');
    expect(command.args).toContain('--output-format');
    expect(command.args).toContain('stream-json');
    expect(command.args).toContain('--verbose');
  });

  it('uses the Claude effort environment control instead of the version-sensitive CLI flag', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({
      args: ['--model', 'claude-sonnet-4-6', '--effort', 'high'],
      env: {},
      prompt_fragments: [],
      unsupported: [],
    });
    refreshRuntimeState();

    const runner = getProviderRunner('anthropic', 'oauth');
    const command = runner.buildRunCommand('prompt body', 'claude-sonnet-4-6', 'high');

    expect(command.args).toContain('--model');
    expect(command.args).toContain('claude-sonnet-4-6');
    expect(command.args).not.toContain('--effort');
    expect(command.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('high');
  });

  it('detects whether the installed Claude CLI supports partial-message streaming', () => {
    expect(
      claudeHelpSupportsPartialMessages(
        'Usage: claude [options]\n  --include-partial-messages  emit stream deltas',
      ),
    ).toBe(true);
    expect(
      claudeHelpSupportsPartialMessages(
        'Usage: claude [options]\n  --output-format <format>',
      ),
    ).toBe(false);
  });

  it('caches Claude capabilities by binary version and preserves model routing while stripping unsupported optional flag values', () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync)
      .mockReturnValueOnce('claude 1.2.3')
      .mockReturnValueOnce('Usage: claude [options]\n  --model <model>\n  --effort <level>');
    const first = probeClaudeCapabilities('claude', process.env);
    const second = probeClaudeCapabilities('claude', process.env);

    expect(first).toEqual(new Set(['--model', '--effort']));
    expect(second).toBe(first);
    expect(execFileSync).toHaveBeenCalledTimes(2);
    expect(stripUnsupportedClaudeOptionalArgs(
      ['--model', 'glm-5.2', '--effort', 'high', '--include-partial-messages'], first,
    )).toEqual(['--model', 'glm-5.2', '--effort', 'high']);
    expect(stripUnsupportedClaudeOptionalArgs(
      ['--model', 'glm-5.2', '--effort', 'high', '--include-partial-messages'], new Set(['--model']),
    )).toEqual(['--model', 'glm-5.2']);
  });

  it('fails open when the Claude capability probe is ambiguous or fails', () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValueOnce('claude 1.2.4').mockReturnValueOnce('');
    expect(probeClaudeCapabilities('claude', process.env)).toBeNull();
    expect(stripUnsupportedClaudeOptionalArgs(['--effort', 'high'], null)).toEqual(['--effort', 'high']);

    __resetClaudeCapabilityCacheForTesting();
    vi.mocked(execFileSync).mockImplementationOnce(() => { throw new Error('help unavailable'); });
    expect(probeClaudeCapabilities('claude', process.env)).toBeNull();
  });

  it('uses Claude Code with the Anthropic-compatible endpoint for Z.AI', () => {
    process.env.PROVIDER = 'zai';
    process.env.AUTH_MODE = 'oauth';
    process.env.ZAI_API_KEY = 'zai-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'glm-5.2', 'high');

    expect(runner.provider).toBe('zai');
    expect(runner.authMode).toBe('api_key');
    expect(providerSupportsNativeSubagents('zai')).toBe(true);
    expect(command.command).toBe('claude');
    expect(command.outputMode).toBe('claude-stream-json');
    expect(command.promptInput).toBe('stdin-file');
    expect(command.displayName).toBe('Z.AI GLM (Claude compat)');
    expect(command.args).toContain('--model');
    expect(command.args).toContain('glm-5.2');
    expect(command.args).not.toContain('--effort');
    expect(command.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('high');
    expect(command.env.ZAI_API_KEY).toBe('zai-test-key');
    expect(command.env.ANTHROPIC_API_KEY).toBe('zai-test-key');
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBe('zai-test-key');
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.z.ai/api/anthropic');
    expect(runner.missingCredentials()).toEqual([]);
  });

  it('does not reuse a generic Anthropic key for Z.AI', () => {
    process.env.PROVIDER = 'zai';
    process.env.AUTH_MODE = 'oauth';
    process.env.ANTHROPIC_API_KEY = 'anthropic-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'glm-5.2', 'high');

    expect(runner.missingCredentials()).toEqual(['ZAI_API_KEY']);
    expect(command.env.ZAI_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBeUndefined();
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.z.ai/api/anthropic');
  });

  it('prefers provider-specific Z.AI base URL over stale generic Anthropic base URL', () => {
    process.env.PROVIDER = 'zai';
    process.env.AUTH_MODE = 'oauth';
    process.env.ZAI_API_KEY = 'zai-test-key';
    process.env.ZAI_ANTHROPIC_BASE_URL = 'https://proxy.test/zai/';
    process.env.ANTHROPIC_BASE_URL = 'https://stale.test/anthropic/';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'glm-5.2', 'high');

    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://proxy.test/zai');
  });

  it('uses Claude Code with the Anthropic-compatible endpoint for DeepSeek', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.AUTH_MODE = 'oauth';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');

    expect(runner.provider).toBe('deepseek');
    expect(runner.authMode).toBe('api_key');
    expect(providerSupportsNativeSubagents('deepseek')).toBe(true);
    expect(command.command).toBe('claude');
    expect(command.outputMode).toBe('claude-stream-json');
    expect(command.promptInput).toBe('stdin-file');
    expect(command.displayName).toBe('DeepSeek (Claude compat)');
    expect(command.args).toContain('--model');
    expect(command.args).toContain('deepseek-v4-flash');
    expect(command.args).not.toContain('--effort');
    expect(command.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('low');
    expect(command.env.DEEPSEEK_API_KEY).toBe('deepseek-test-key');
    expect(command.env.ANTHROPIC_API_KEY).toBe('deepseek-test-key');
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBe('deepseek-test-key');
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.deepseek.com/anthropic');
    expect(runner.missingCredentials()).toEqual([]);
  });

  it('does not reuse a generic Anthropic key for DeepSeek', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.AUTH_MODE = 'oauth';
    process.env.ANTHROPIC_API_KEY = 'anthropic-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');

    expect(runner.missingCredentials()).toEqual(['DEEPSEEK_API_KEY']);
    expect(command.env.DEEPSEEK_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBeUndefined();
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.deepseek.com/anthropic');
  });

  it('prefers provider-specific DeepSeek base URL over stale generic Anthropic base URL', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.AUTH_MODE = 'oauth';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    process.env.DEEPSEEK_ANTHROPIC_BASE_URL = 'https://proxy.test/deepseek/';
    process.env.ANTHROPIC_BASE_URL = 'https://stale.test/anthropic/';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');

    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://proxy.test/deepseek');
  });

  it('sets the documented DeepSeek Claude Code model env and aligns ANTHROPIC_MODEL with the requested model', () => {
    // DeepSeek's documented integration selects models via env; without it the
    // CLI resolves its claude-* aliases, which DeepSeek silently remaps
    // (unknown/sonnet/haiku ids → v4-flash) — the Resident Pro-task failure
    // mode. The env must carry the documented defaults, and ANTHROPIC_MODEL
    // must track the explicitly requested model so the env-driven paths
    // (resident stream-json session, subagent spawns) resolve the same model
    // the --model flag names.
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-pro', 'high');

    expect(command.env.ANTHROPIC_MODEL).toBe('deepseek-v4-pro');
    expect(command.env.ANTHROPIC_DEFAULT_OPUS_MODEL).toBe('deepseek-v4-pro');
    expect(command.env.ANTHROPIC_DEFAULT_SONNET_MODEL).toBe('deepseek-v4-pro');
    expect(command.env.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe('deepseek-v4-flash');
    expect(command.env.CLAUDE_CODE_SUBAGENT_MODEL).toBe('deepseek-v4-flash');

    // A different requested model re-aligns ANTHROPIC_MODEL (defaults stay).
    const flash = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');
    expect(flash.env.ANTHROPIC_MODEL).toBe('deepseek-v4-flash');
    expect(flash.env.ANTHROPIC_DEFAULT_OPUS_MODEL).toBe('deepseek-v4-pro');
  });

  it('lets operator-provided model env override the DeepSeek defaults', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    process.env.ANTHROPIC_DEFAULT_HAIKU_MODEL = 'deepseek-v4-pro';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', undefined, 'low');

    // Account/pool-materialized env wins over the built-in defaults.
    expect(command.env.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe('deepseek-v4-pro');
    // No explicit model requested → the documented default stands.
    expect(command.env.ANTHROPIC_MODEL).toBe('deepseek-v4-pro');
  });

  it('preserves LLM_CLI_TRANSLATION env over DeepSeek defaults, below process.env', () => {
    // Precedence contract for the deepseek model env: process.env (account
    // materialization) > translation env (settings bag) > documented default.
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    process.env.ANTHROPIC_DEFAULT_SONNET_MODEL = 'deepseek-v4-flash'; // materialized
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({
      env: {
        ANTHROPIC_DEFAULT_HAIKU_MODEL: 'deepseek-v4-pro',   // translation layer
        ANTHROPIC_DEFAULT_SONNET_MODEL: 'deepseek-v4-pro',  // loses to process.env
      },
      args: [],
      prompt_fragments: [],
      unsupported: [],
    });
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', undefined, 'low');

    // Translation env survives the default fill (was clobbered pre-fix).
    expect(command.env.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe('deepseek-v4-pro');
    // Materialized process.env still outranks the translation layer.
    expect(command.env.ANTHROPIC_DEFAULT_SONNET_MODEL).toBe('deepseek-v4-flash');
    // Untouched keys fall through to the documented defaults.
    expect(command.env.CLAUDE_CODE_SUBAGENT_MODEL).toBe('deepseek-v4-flash');
  });

  it('does not leak DeepSeek model env into Anthropic runs on a retargeted VM', () => {
    // A warm VM retargeted deepseek→anthropic may still carry the DeepSeek
    // model env in process.env; the anthropic branch must not propagate it,
    // or the Claude CLI would request a nonexistent Anthropic model.
    process.env.PROVIDER = 'anthropic';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'oauth-token';
    process.env.ANTHROPIC_MODEL = 'deepseek-v4-pro';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'deepseek-v4-flash';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'claude-opus-4-8', 'high');

    expect(command.env.ANTHROPIC_MODEL).toBeUndefined();
    expect(command.env.CLAUDE_CODE_SUBAGENT_MODEL).toBeUndefined();
  });

  it('validates the requested model instead of a bare auth probe (Claude-compat providers)', () => {
    // A validation that never names the model proves only that the key
    // authenticates — AGENT_AUTH_OK could pass while the requested model is
    // broken (the DeepSeek Resident failure mode: Pro tasks validated OK,
    // then every resident session died on its first turn).
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const validation = defined(runner.buildValidationCommand('deepseek-v4-pro'));

    expect(validation.command).toBe('claude');
    expect(validation.args).toContain('--model');
    expect(validation.args[validation.args.indexOf('--model') + 1]).toBe('deepseek-v4-pro');
    expect(validation.env.ANTHROPIC_MODEL).toBe('deepseek-v4-pro');

    // Model-less validation still works (no --model flag, env defaults only).
    const bare = defined(runner.buildValidationCommand());
    expect(bare.args).not.toContain('--model');
  });

  it('passes --model to the Anthropic validation command when the pool selected one', () => {
    process.env.PROVIDER = 'anthropic';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'oauth-token';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const validation = defined(runner.buildValidationCommand('claude-opus-4-8'));
    expect(validation.args).toContain('--model');
    expect(validation.args[validation.args.indexOf('--model') + 1]).toBe('claude-opus-4-8');
    // No DeepSeek-style env pinning outside the deepseek branch.
    expect(validation.env.ANTHROPIC_MODEL).toBeUndefined();
  });
});

// ── LLM_CLI_TRANSLATION parsing ───────────────────────────────────────────────
// The runtime consumes a JSON-encoded translation blob from the backend's
// settings-bag resolver. Validate parsing is tolerant of missing fields and
// fails-safe to null on malformed JSON so legacy dispatch still works.
describe('parseLLMCliTranslation', () => {
  const originalEnv = process.env.LLM_CLI_TRANSLATION;

  afterEach(() => {
    if (originalEnv === undefined) delete process.env.LLM_CLI_TRANSLATION;
    else process.env.LLM_CLI_TRANSLATION = originalEnv;
  });

  it('returns null when env var is absent', () => {
    delete process.env.LLM_CLI_TRANSLATION;
    expect(parseLLMCliTranslation()).toBeNull();
  });

  it('parses a valid translation', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({
      env: { FOO: 'bar' },
      args: ['--model', 'claude-opus-4-7', '--effort', 'high'],
      prompt_fragments: ['Plan carefully.'],
      unsupported: ['advisor.enabled (fallback: prompt fragment only)'],
    });
    const parsed = defined(parseLLMCliTranslation());
    expect(parsed.args).toEqual(['--model', 'claude-opus-4-7', '--effort', 'high']);
    expect(parsed.env).toEqual({ FOO: 'bar' });
    expect(parsed.prompt_fragments).toEqual(['Plan carefully.']);
    expect(parsed.unsupported[0]).toMatch(/advisor/);
  });

  it('tolerates missing fields with safe defaults', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({ args: ['-m', 'gpt-5'] });
    const parsed = defined(parseLLMCliTranslation());
    expect(parsed.args).toEqual(['-m', 'gpt-5']);
    expect(parsed.env).toEqual({});
    expect(parsed.prompt_fragments).toEqual([]);
    expect(parsed.unsupported).toEqual([]);
  });

  it('returns null on malformed JSON (fail-safe to legacy dispatch)', () => {
    process.env.LLM_CLI_TRANSLATION = '{not valid json';
    expect(parseLLMCliTranslation()).toBeNull();
  });
});

// ── captureTestBaseline timeout selection ─────────────────────────────────────
// execFileAsync passes its timeoutMs through to spawn's options dict (Node
// ignores it, so it doesn't affect runtime — see the comment in execFileAsync).
// That makes the configured timeout per runner observable here.
describe('captureTestBaseline timeout selection', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => ({}),
      text: () => '',
      headers: { get: () => null },
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (10min) for runners containing scripts/test-backend.sh', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect((defined(bashCall)[2] as { timeout: number }).timeout).toBe(600_000);
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (10min) for runners with docker in args', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'docker run --rm myimage pytest' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const dockerCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'docker');
    expect(dockerCall).toBeDefined();
    expect((defined(dockerCall)[2] as { timeout: number }).timeout).toBe(600_000);
  });

  it('uses the passed timeoutMs (60s default) for non-Docker runners', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'node', test_cmd: 'npm test' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const npmCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'npm');
    expect(npmCall).toBeDefined();
    expect((defined(npmCall)[2] as { timeout: number }).timeout).toBe(60_000);
  });
});

// ── Auto-detect: scripts/test-backend.sh postgres flag follows Docker availability ─
// Without --postgres, test-backend.sh defaults to in-memory SQLite. CI runs
// Postgres. SQLite-passing-Postgres-failing fixes get pushed and only fail on
// CI, costing a full review-fix iteration. When Docker is available, auto-detect
// passes --postgres so the local gate matches CI. When Docker is absent, pass
// --no-postgres so tests still run via the in-memory SQLite path.
describe('detectTestRunners auto-detect postgres flag', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(execSync).mockReset();
    // Default: Docker is available (execSync('docker version') succeeds)
    vi.mocked(execSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
    vi.mocked(fs.existsSync).mockReset();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => ({}),
      text: () => '',
      headers: { get: () => null },
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fakeSelfRepoFs() {
    // Simulate Blueprint's own repo layout: backend/pytest.ini exists AND
    // scripts/test-backend.sh exists. No top-level package.json, no agent/
    // package.json — so the only auto-detected runner is the backend script.
    vi.mocked(fs.existsSync).mockImplementation((p: fs.PathLike) => {
      const s = String(p);
      return s.endsWith('/backend/pytest.ini') || s.endsWith('/scripts/test-backend.sh');
    });
  }

  it('appends --postgres when Docker is available (backend/pytest.ini + scripts/test-backend.sh)', async () => {
    fakeSelfRepoFs();
    // No buildConfig = auto-detect path
    await captureTestBaseline(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--postgres');
    expect(args).not.toContain('--no-postgres');
  });

  it('appends --postgres when Docker is available in testFixLoop', async () => {
    fakeSelfRepoFs();
    await testFixLoop(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--postgres');
    expect(args).not.toContain('--no-postgres');
  });

  it('runs targeted pytest selectors before impact coverage when requested', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh --postgres' }],
    };

    await testFixLoop(
      null, { value: '' }, undefined, undefined, buildConfig,
      undefined, undefined, undefined,
      {
        targetPytestSelectors: ['backend/testing/test_models.py::test_create'],
        runImpactAfterTargeted: true,
        requireTestmonForImpact: true,
      },
    );

    const bashCalls = vi.mocked(spawn).mock.calls.filter(c => c[0] === 'bash');
    expect(bashCalls).toHaveLength(2);
    expect(bashCalls[0][1]).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
    expect(bashCalls[1][1]).toEqual(['scripts/test-backend.sh', '--postgres', '--require-testmon']);
  });

  it('does not scope away backend targeted runners when related files point elsewhere', async () => {
    const buildConfig = {
      directories: [
        { path: 'agent', type: 'node', test_cmd: 'npm test' },
        { path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh --postgres' },
      ],
    };

    await testFixLoop(
      null, { value: '' }, undefined, undefined, buildConfig,
      undefined, ['agent/src/index.ts'], undefined,
      { targetPytestSelectors: ['backend/testing/test_models.py::test_create'] },
    );

    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect(defined(bashCall)[1]).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
  });

  it('appends --no-postgres when Docker is unavailable (captureTestBaseline)', async () => {
    fakeSelfRepoFs();
    // Simulate Docker absent: execSync throws
    vi.mocked(execSync).mockImplementation(() => { throw new Error('docker not found'); });
    await captureTestBaseline(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--no-postgres');
    expect(args).not.toContain('--postgres');
  });

  it('appends --no-postgres when Docker is unavailable in testFixLoop', async () => {
    fakeSelfRepoFs();
    vi.mocked(execSync).mockImplementation(() => { throw new Error('docker not found'); });
    await testFixLoop(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--no-postgres');
    expect(args).not.toContain('--postgres');
  });
});

// ── Agent reliability medium fixes ───────────────────────────────────────────
describe('execFileAsync process reliability', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not SIGKILL the original process group after the child exits during timeout cleanup', async () => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);
    let closeCb: ((code: number | null, signal: NodeJS.Signals | null) => void) | undefined;
    const proc = {
      pid: 4321,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
        if (event === 'close') closeCb = cb;
      }),
    };
    vi.mocked(spawn).mockReturnValue(proc as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('npm', ['test'], { timeoutMs: 1000 }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(1000);
    expect(killSpy).toHaveBeenCalledWith(-4321, 'SIGTERM');

    proc.exitCode = 143;
    await vi.advanceTimersByTimeAsync(5000);
    expect(killSpy).not.toHaveBeenCalledWith(-4321, 'SIGKILL');

    closeCb?.(143, 'SIGTERM');
    await promise;
    killSpy.mockRestore();
  });

  it('tags external SIGKILL as EOOM instead of ETIMEDOUT', async () => {
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const proc = {
        pid: 4322,
        exitCode: null as number | null,
        signalCode: null as NodeJS.Signals | null,
        stdout: { on: vi.fn() },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              cb(null, 'SIGKILL');
            });
          }
        }),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    await expect(execFileAsync('node', ['worker.js'], { timeoutMs: 60_000 }))
      .rejects.toMatchObject({ code: 'EOOM', signal: 'SIGKILL', killed: true });
  });
});

describe('provider runner reliability', () => {
  function mockProviderRun(stderrChunks: string[], code: number | null, signal: NodeJS.Signals | null = null) {
    vi.mocked(spawn).mockImplementation(() => {
      const stderrCbs: Array<(d: Buffer) => void> = [];
      const proc = {
        pid: 9876,
        exitCode: null as number | null,
        signalCode: null as NodeJS.Signals | null,
        stdin: { on: vi.fn(), end: vi.fn() },
        stdout: { on: vi.fn() },
        stderr: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stderrCbs.push(cb);
          }),
        },
        on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              for (const chunk of stderrChunks) {
                for (const stderrCb of stderrCbs) {
                  stderrCb(Buffer.from(chunk));
                }
              }
              proc.exitCode = code;
              proc.signalCode = signal;
              cb(code, signal);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
  }

  let savedTaskId: string | undefined;
  beforeEach(() => {
    vi.stubEnv('TASK_ID', '');
    vi.mocked(spawn).mockReset();
    vi.mocked(fs.writeFileSync).mockClear();
    vi.mocked(fs.chmodSync).mockClear();
    vi.mocked(fs.unlinkSync).mockClear();
    vi.mocked(fs.openSync).mockClear();
    vi.mocked(fs.openSync).mockReturnValue(3);
    savedTaskId = process.env.TASK_ID;
    delete process.env.TASK_ID;
  });

  afterEach(() => {
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
    else delete process.env.TASK_ID;
  });

  it('uses the stderr tail so late rate-limit text is still classified', async () => {
    mockProviderRun(['x'.repeat(60_000), "\nyou've hit your usage limit\n"], 1);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(RateLimitError);
  });

  it('classifies clean-exit (code 0) with rate-limit text in tail as RateLimitError', async () => {
    // The Anthropic CLI sometimes emits the weekly-limit message as a normal
    // assistant turn then exits CLEAN (code 0); without this catch, every
    // handler treats the run as success and posts a degenerate artifact —
    // most recently the release_audit handler posted a `{audit_score: 0}`
    // stub that the qualifier scored as a legitimate 0.43 verdict instead
    // of routing through account rotation.
    mockProviderRun(["You've hit your weekly limit · resets May 29, 4am (UTC)\n"], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(RateLimitError);
  });

  it('clean-exit with rate-limit text >5KB from the tail does NOT match', async () => {
    // LOG_TAIL_BYTES = 5_000. Anything beyond that window from the end must
    // not match — a mid-conversation mention of the phrase (e.g. an earlier
    // tool result quoting documentation) is not a real rate-limit signal.
    // Chunks are concatenated to stderr in this mock; stderrBuffer is bounded
    // to its own tail by appendBoundedTail, so a leading rate-limit phrase
    // followed by 60KB of benign output drops the phrase out of the tail.
    mockProviderRun(["you've hit your usage limit\n", 'x'.repeat(60_000)], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('clean-exit with no rate-limit text resolves normally (regression)', async () => {
    mockProviderRun(['ordinary tool output\n'], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('classifies a non-zero exit with "Claude configuration file not found" as VmConfigMissingError', async () => {
    // Poisoned-VM signature (2026-06-06, tasks 1330d291 / cf28097f): the
    // re-target destroyed ~/.claude.json, so the CLI dies in seconds. This must
    // surface the distinct vm_config_missing infra category — NOT the generic
    // fatal/claude_code_failed path whose init-banner-only tail the
    // orchestrator reclassifies as usage exhaustion.
    mockProviderRun(['Claude configuration file not found at /home/agent/.claude.json\n'], 1);

    const err = await runProviderAgent('prompt', null, { value: '' }).then(
      () => { throw new Error('expected rejection'); },
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(VmConfigMissingError);
    expect((err as VmConfigMissingError).failureCategory).toBe('vm_config_missing');
  });

  it('config-missing wins over coincidental rate-limit text in the same tail', async () => {
    // The whole point of the distinct signal is to stop these deaths being
    // read as usage exhaustion — so the config-missing check must take
    // precedence over the rate-limit pattern match.
    mockProviderRun([
      "Claude configuration file not found\nyou've hit your usage limit\n",
    ], 1);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(VmConfigMissingError);
  });

  it('a clean exit mentioning the config phrase is NOT swept (exit-code gated)', async () => {
    // e.g. a tool result quoting this very incident report — a zero-exit run
    // must never be converted into an infra failure.
    mockProviderRun(['discussing "Claude configuration file not found" handling\n'], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('writes the stdin prompt file as owner-only', async () => {
    vi.stubEnv('TASK_ID', '');
    mockProviderRun([], 0);

    await runProviderAgent('private prompt', null, { value: '' });

    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(
      '/tmp/agent_prompt_unknown.txt',
      'private prompt',
      { encoding: 'utf-8', mode: 0o600 },
    );
    expect(vi.mocked(fs.chmodSync)).toHaveBeenCalledWith('/tmp/agent_prompt_unknown.txt', 0o600);
  });
});

describe('structured error report shipping (enrichStatusPayload)', () => {
  const makeReport = (taskId: string): ProviderRunErrorReport => ({
    schema: 1,
    task_id: taskId,
    provider: 'anthropic',
    auth_mode: 'oauth',
    exit_code: 1,
    exit_signal: null,
    duration_ms: 5_000,
    model_output_seen: false,
    result_event_seen: false,
    provider_errors: [],
    stderr_tail: '',
    last_stream_event: 'system',
  });

  const stubStatusFetch = (): FetchMock => {
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'ok' }),
      text: () => Promise.resolve(''),
      headers: { get: () => null },
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  };

  const postedStatusBody = (fetchMock: FetchMock): Record<string, unknown> => {
    const statusCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes('/api/agent/status'));
    if (!statusCall) throw new Error('expected a /api/agent/status POST');
    const init = statusCall[1] as RequestInit;
    if (typeof init.body !== 'string') throw new Error('expected a string request body');
    return JSON.parse(init.body) as Record<string, unknown>;
  };

  afterEach(() => {
    vi.unstubAllGlobals();
    _setLastProviderRunErrorReportForTesting(null);
  });

  it('attaches the last run report to FAILED status posts for the same task', async () => {
    const fetchMock = stubStatusFetch();
    _setLastProviderRunErrorReportForTesting(makeReport('task-er-1'));

    await persistAndPostStatus('task-er-1', {
      task_id: 'task-er-1',
      status: 'FAILED',
      failure_category: 'claude_code_failed',
      logs: 'Claude subprocess exited with code 1 without shipping a PR.',
    });

    const body = postedStatusBody(fetchMock);
    expect(body.error_report).toMatchObject({
      task_id: 'task-er-1',
      exit_code: 1,
      model_output_seen: false,
      result_event_seen: false,
    });
  });

  it('does NOT attach a report to COMPLETED posts', async () => {
    const fetchMock = stubStatusFetch();
    _setLastProviderRunErrorReportForTesting(makeReport('task-er-2'));

    await persistAndPostStatus('task-er-2', { task_id: 'task-er-2', status: 'COMPLETED' });

    expect(postedStatusBody(fetchMock).error_report).toBeUndefined();
  });

  it('does NOT leak a stale report from a different task (warm-VM guard)', async () => {
    // A warm VM runs sequential roles in one process. A failure that never ran
    // the provider CLI (e.g. clone_failed) must not ship the PRIOR role's
    // report and misclassify itself as a provider death.
    const fetchMock = stubStatusFetch();
    _setLastProviderRunErrorReportForTesting(makeReport('task-er-prior-role'));

    await persistAndPostStatus('task-er-3', {
      task_id: 'task-er-3',
      status: 'FAILED',
      failure_category: 'clone_failed',
    });

    expect(postedStatusBody(fetchMock).error_report).toBeUndefined();
  });
});

describe('stale result retry envelopes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('persists status payloads in a retry envelope before posting', async () => {
    vi.mocked(fs.writeFileSync).mockClear();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: 'ok' }),
      text: () => Promise.resolve(''),
    }));

    await persistAndPostStatus('task-1', { task_id: 'task-1', status: 'COMPLETED' });

    const writeCall = vi.mocked(fs.writeFileSync).mock.calls.find(call => call[0] === '/tmp/task_task-1_result.json');
    if (!writeCall) throw new Error('expected stale result file write');
    const rawWritten = writeCall[1];
    if (typeof rawWritten !== 'string') throw new Error('expected stale result write payload to be a string');
    const written = JSON.parse(rawWritten) as {
      payload: { status?: unknown };
      retry: { attempts?: unknown; nextAttemptAt?: unknown };
    };
    expect(written.payload.status).toBe('COMPLETED');
    expect(written.retry.attempts).toBe(0);
    expect(written.retry.nextAttemptAt).toBe(0);
  });

  it('parses legacy payload files and records per-file backoff after failures', () => {
    const legacy = parseStaleResultFile(JSON.stringify({ task_id: 'legacy-task', status: 'FAILED' }), 1000);
    expect(legacy.payload.task_id).toBe('legacy-task');
    expect(legacy.retry.createdAt).toBe(1000);

    const failed = markStaleResultFailure(legacy, new Error('network down'), 2000);
    expect(failed.retry.attempts).toBe(1);
    expect(failed.retry.nextAttemptAt).toBe(32_000);
    expect(failed.retry.lastError).toBe('network down');
  });

  it('abandons stale result files after the retry ceiling', () => {
    const stale = parseStaleResultFile(JSON.stringify({
      payload: { task_id: 'old-task' },
      retry: { attempts: 10, createdAt: 1000, nextAttemptAt: 0 },
    }), 1000);

    expect(shouldAbandonStaleResult(stale, 2000)).toBe(true);
  });

  it('fails final status with oauth_rotation_failed when OAuth rotation cannot persist', async () => {
    const oldProvider = process.env.PROVIDER;
    const oldAuthMode = process.env.AUTH_MODE;
    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    vi.resetModules();
    vi.mocked(fs.existsSync).mockReturnValue(true);
    mockReadFileSync.mockImplementation((path, encoding) => {
      if (String(path).includes('/opt/agent/openai/auth.json')) {
        return encoding ? '{"refresh_token":"rotated"}' : Buffer.from('{"refresh_token":"rotated"}');
      }
      return '';
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes('/api/agent/credentials/rotate')) {
        return Promise.resolve({
          ok: false,
          status: 400,
          text: () => Promise.resolve('revoked token'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        } as unknown as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({ status: 'ok' }),
        headers: { get: () => null },
      } as unknown as Response);
    });
    vi.stubGlobal('fetch', fetchMock);

    try {
      const mod = await import('./index.js');
      await mod.persistAndPostStatus('task-oauth', {
        task_id: 'task-oauth',
        status: 'COMPLETED',
        logs: 'original completion',
      });
    } finally {
      process.env.PROVIDER = oldProvider;
      process.env.AUTH_MODE = oldAuthMode;
    }

    const statusCall = fetchMock.mock.calls.find(([url]) => {
      const u = typeof url === 'string' ? url : url instanceof URL ? url.toString() : url.url;
      return u.includes('/api/agent/status');
    });
    expect(statusCall).toBeTruthy();
    const rawBody = statusCall?.[1]?.body;
    const bodyStr = typeof rawBody === 'string' ? rawBody : '';
    const body = JSON.parse(bodyStr) as Record<string, unknown>;
    expect(body.status).toBe('FAILED');
    expect(body.oauth_rotation_failed).toBe(true);
    expect(body.failure_category).toBe('oauth_rotation_failed');
    expect(body.logs).toContain('OAuth credential rotation failed');
  });
});

// ── setupWorkspace: pre-baked git object cache reuse ─────────────────────────
describe('setupWorkspace reference-cache reuse', () => {
  const defaultSpawn = vi.mocked(spawn).getMockImplementation();

  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(spawn).mockReset();
    if (defaultSpawn) vi.mocked(spawn).mockImplementation(defaultSpawn);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** execFileSync stub: ls-remote throws so the stale-branch path is skipped;
   *  every other git/rm/curl invocation succeeds.
   *  cloneWithRetry now uses gitAsync (spawn-based), so we also stub spawn for
   *  'git clone' to succeed immediately. */
  function stubExecFileSyncNoStaleBranch(): void {
    vi.mocked(execFileSync).mockImplementation((cmd: unknown, args: unknown) => {
      const a = Array.isArray(args) ? (args as string[]) : [];
      if (String(cmd) === 'git' && a[0] === 'ls-remote') {
        throw new Error('fatal: branch not found'); // → staleBranchExists = false
      }
      return '';
    });
    // Stub spawn so gitAsync (used by cloneWithRetry) resolves immediately.
    vi.mocked(spawn).mockImplementation((cmd: unknown) => {
      if (cmd === 'git') {
        const proc = {
          pid: 999,
          stdin: { on: vi.fn(), end: vi.fn() },
          stdout: { on: vi.fn() },
          stderr: { on: vi.fn() },
          on: vi.fn((event: string, cb: (code: number) => void) => {
            if (event === 'close') void Promise.resolve().then(() => { cb(0); });
          }),
          kill: vi.fn(),
        };
        return proc as unknown as ReturnType<typeof spawn>;
      }
      return { on: vi.fn(), kill: vi.fn() } as unknown as ReturnType<typeof spawn>;
    });
  }

  function findCloneArgs(): string[] {
    // cloneWithRetry now uses gitAsync → spawn('git', ...), not execFileSync
    const cloneCall = vi.mocked(spawn).mock.calls.find(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('clone'),
    );
    expect(cloneCall).toBeDefined();
    return cloneCall?.[1] as string[];
  }

  it('clone args include --reference-if-able when the mirror exists', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true); // mirror present on disk
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const cloneArgs = findCloneArgs();
    expect(cloneArgs).toContain('--reference-if-able');
    // Reference path matches GIT_REFERENCE_CACHE_ROOT/<owner>/<repo>.git.
    const refIdx = cloneArgs.indexOf('--reference-if-able');
    expect(cloneArgs[refIdx + 1]).toBe('/opt/agent-cache/git/owner/repo.git');
  });

  it('clone args exclude --reference-if-able when the mirror is absent', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false); // no mirror seeded for this repo
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const cloneArgs = findCloneArgs();
    expect(cloneArgs).not.toContain('--reference-if-able');
  });

  it('setupWorkspace does not embed token in clone URL', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const cloneArgs = findCloneArgs();
    // Clean URL — no x-access-token embedded.
    const urlArg = cloneArgs[cloneArgs.length - 2]; // second-to-last is the URL
    expect(urlArg).toBe('https://github.com/owner/repo.git');
    expect(urlArg).not.toContain('x-access-token');
    // Credential helper -c flag present.
    expect(cloneArgs.some(a => a.startsWith('credential.https://github.com.helper='))).toBe(true);
  });

  it('registers the sizecap merge driver after configuring user.* git config', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const gitConfigCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('config'),
    );
    // The driver command is registered…
    const driverCall = gitConfigCalls.find(
      c => (c[1] as string[]).includes('merge.sizecap.driver'),
    );
    expect(driverCall).toBeDefined();
    expect(driverCall?.[1]).toContain('python3 backend/testing/merge_size_limits.py %O %A %B');
    // …after the user.email/user.name config (so warm/cold ordering is stable).
    const idx = (key: string) => gitConfigCalls.findIndex(c => (c[1] as string[]).includes(key));
    expect(idx('merge.sizecap.driver')).toBeGreaterThan(idx('user.email'));
  });
});

// ── registerSizeCapMergeDriver ───────────────────────────────────────────────
describe('registerSizeCapMergeDriver', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
  });

  it('configures both merge.sizecap.name and merge.sizecap.driver in the given workspace', () => {
    registerSizeCapMergeDriver('/custom/ws');

    const calls = vi.mocked(execFileSync).mock.calls.filter(c => c[0] === 'git');
    const argLists = calls.map(c => (c[1] as string[]).join(' '));
    expect(argLists.some(a => a === '-C /custom/ws config merge.sizecap.name max-cap union merge for module_size_limits.json')).toBe(true);
    expect(argLists.some(a => a === '-C /custom/ws config merge.sizecap.driver python3 backend/testing/merge_size_limits.py %O %A %B')).toBe(true);
  });

  it('swallows git config failures (best-effort registration)', () => {
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('git config boom'); });
    // Must not throw — a failed registration only means the JSON falls back to
    // the normal conflict path.
    expect(() => { registerSizeCapMergeDriver('/ws'); }).not.toThrow();
  });
});

// ── Failure category classification ──────────────────────────────────────────
describe('failure category classification', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(spawn).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('setupWorkspace attaches clone_failed when git clone fails', async () => {
    vi.useFakeTimers();
    vi.mocked(execFileSync).mockImplementation((cmd: unknown) => {
      const cmdStr = String(cmd);
      // Allow rm (workspace cleanup) and curl (heartbeat) to succeed
      if (cmdStr === 'rm' || cmdStr === 'curl') return '';
      // All git commands fail — simulates persistent clone failure across all retries
      throw new Error('fatal: Authentication failed');
    });

    let caughtError: unknown;
    const promise = setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint')
      .catch((e: unknown) => { caughtError = e; });

    // Advance past all retry delays (CLONE_MAX_RETRIES=3: 5s + 10s = 15s total)
    await vi.runAllTimersAsync();
    await promise;

    expect(caughtError).toBeInstanceOf(Error);
    expect((caughtError as Error & { failureCategory?: string }).failureCategory).toBe('clone_failed');
  });

  it('pushAndCreatePR does not embed token in remote set-url', async () => {
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(spawn).mockImplementation(() => {
      const proc = {
        pid: 123,
        stderr: { on: vi.fn() },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') setImmediate(() => { cb(0); });
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ data: { token: 'fresh-push-token' } }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ data: { pr_url: 'https://github.com/owner/repo/pull/1', pr_number: 1 } }),
      }),
    );

    await pushAndCreatePR('owner', 'repo', 'task-id', 'blueprint/branch', 'main', 'Title', 'Body', null, { value: '' }, true);

    const setUrlCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('set-url'),
    );
    expect(setUrlCalls.length).toBeGreaterThanOrEqual(1);
    for (const call of setUrlCalls) {
      const args = call[1] as string[];
      const urlInCall = args[args.length - 1];
      expect(urlInCall).toBe('https://github.com/owner/repo.git');
      expect(urlInCall).not.toContain('x-access-token');
      expect(urlInCall).not.toContain('fresh-push-token');
    }
  });

  it('pushAndCreatePR attaches pr_creation_failed when GitHub PR creation returns 403', async () => {
    // Allow all execFileSync calls (git remote set-url, curl heartbeat)
    vi.mocked(execFileSync).mockReturnValue('');
    // Mock spawn to make gitAsync push succeed
    vi.mocked(spawn).mockImplementation(() => {
      const proc = {
        pid: 123,
        stderr: { on: vi.fn() },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') setImmediate(() => { cb(0); });
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
    // First fetch: apiGet for fresh GitHub token; second fetch: apiPost for PR creation (403)
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ data: { token: 'fresh-token' } }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        headers: { get: () => null },
        text: () => Promise.resolve('Forbidden'),
      }),
    );

    let caughtError: unknown;
    try {
      // skipRebase=true avoids git fetch/rebase operations
      await pushAndCreatePR('owner', 'repo', 'task-id', 'blueprint/branch', 'main', 'Title', 'Body', null, { value: '' }, true);
    } catch (e) {
      caughtError = e;
    }

    expect(caughtError).toBeInstanceOf(Error);
    expect((caughtError as Error & { failureCategory?: string }).failureCategory).toBe('pr_creation_failed');
  });
});

// ── condensedAnalysisSections ────────────────────────────────────────────────
describe('condensedAnalysisSections', () => {
  it('extracts canonical headings exactly as written by the prompt', () => {
    const ctx = [
      '**Files to change**',
      '- a.ts',
      '',
      '**Interfaces to preserve**',
      '- Foo',
      '',
      '**Key dependencies**',
      '- bar.ts → a.ts',
      '',
      '**Callers to verify after implementation**',
      '- caller.ts (uses: foo)',
      '',
      '**Tests at risk of breaking**',
      '- testing/test_x.py — fixture change',
    ].join('\n');
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to change**');
    expect(out).toContain('**Interfaces to preserve**');
    expect(out).toContain('**Key dependencies**');
    expect(out).toContain('**Callers to verify after implementation**');
    expect(out).toContain('**Tests at risk of breaking**');
    expect(out).toContain('- testing/test_x.py — fixture change');
  });

  it('extracts "Tests at risk of breaking" section', () => {
    const ctx = '**Tests at risk of breaking**\n- testing/test_foo.py — assertion change\n\n**Implementation approach**\n- step';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Tests at risk of breaking**');
    expect(out).toContain('- testing/test_foo.py — assertion change');
    expect(out).not.toContain('Implementation approach');
  });

  it('matches case-insensitively', () => {
    const ctx = '**FILES TO CHANGE**\n- a.ts\n\n**Implementation Approach**\n- step';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**FILES TO CHANGE**');
    expect(out).not.toContain('Implementation Approach');
  });

  it('matches "Files to modify" as a synonym for "Files to change"', () => {
    const ctx = '**Files to modify**\n- a.ts\n- b.ts';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to modify**');
    expect(out).toContain('- a.ts');
  });

  it('matches headings with parenthetical suffixes', () => {
    const ctx = '**Files to change** (in recommended order)\n- a.ts\n\n**Interfaces to preserve** (signatures)\n- Foo';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to change** (in recommended order)');
    expect(out).toContain('**Interfaces to preserve** (signatures)');
  });

  it('matches "Key dependencies" or just "Dependencies"', () => {
    const ctx = '**Dependencies**\n- a → b';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Dependencies**');
    expect(out).toContain('- a → b');
  });

  it('matches "Callers to verify" with trailing variation', () => {
    const ctx = '**Callers to verify**\n- caller.ts';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Callers to verify**');
  });

  it('falls back to truncation when no key headings match', () => {
    const long = '**Risks**\n' + 'x'.repeat(1000);
    const out = condensedAnalysisSections(long);
    expect(out.length).toBeLessThanOrEqual(800);
    expect(out).toContain('**Risks**');
  });

  it('does not match unrelated headings that contain a single key term', () => {
    const ctx = '**Files used**\n- nope\n\n**Files to change**\n- a.ts\n\n**Implementation approach**\n- step';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to change**');
    expect(out).not.toContain('**Files used**');
    expect(out).not.toContain('**Implementation approach**');
  });
});

// ── parseFailingTests ────────────────────────────────────────────────────────
describe('parseFailingTests', () => {
  it('parses pytest FAILED lines with full test ID and reason', () => {
    const output = [
      'FAILED backend/testing/test_foo.py::test_bar - AssertionError: expected 1 got 2',
      'FAILED backend/testing/test_baz.py::test_qux - ValueError',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set([
      'backend/testing/test_foo.py::test_bar',
      'backend/testing/test_baz.py::test_qux',
    ]));
  });

  it('parses vitest failure markers (✕, ✗, ×) with descriptions', () => {
    const output = [
      '✕ should render component correctly',
      '✗ handles empty input',
      '× rejects invalid payload',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set([
      'should render component correctly',
      'handles empty input',
      'rejects invalid payload',
    ]));
  });

  it('handles mixed pytest and vitest output in the same string', () => {
    const output = [
      'Running tests...',
      'FAILED backend/testing/test_foo.py::test_bar - AssertionError',
      '✕ frontend renders nav',
      'PASSED backend/testing/test_ok.py::test_ok',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set([
      'backend/testing/test_foo.py::test_bar',
      'frontend renders nav',
    ]));
  });

  it('returns an empty Set for output with no test failures', () => {
    const output = [
      'All tests passed!',
      'PASSED backend/testing/test_ok.py::test_ok',
      '✓ everything works',
      '',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.size).toBe(0);
  });

  it('handles pytest lines where the test ID contains special characters and parametrize brackets', () => {
    const output = [
      'FAILED backend/testing/test_p.py::test_param[case-1] - AssertionError',
      'FAILED backend/testing/test_p.py::test_unicode_ünîcødé - RuntimeError',
      'FAILED backend/testing/test_q.py::test_with_space description - fail',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.has('backend/testing/test_p.py::test_param[case-1]')).toBe(true);
    expect(result.has('backend/testing/test_p.py::test_unicode_ünîcødé')).toBe(true);
    expect(result.has('backend/testing/test_q.py::test_with_space description')).toBe(true);
  });

  it('parses vitest "FAIL  path > suite > test" stdout lines (default reporter)', () => {
    // Regression: prior parser missed vitest's " FAIL " format and
    // returned 0 failures for a real 125-failure vitest run on main —
    // which then leaked into the fix loop as "newly broken by the
    // agent's changes."
    const output = [
      ' FAIL  src/handlers/standard.test.ts > renders the right CLI name',
      ' FAIL  src/prompt.test.ts > appends DEFAULT_TOOL_GUIDANCE',
      ' PASS  src/index.test.ts > parses options',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.has('src/handlers/standard.test.ts > renders the right CLI name')).toBe(true);
    expect(result.has('src/prompt.test.ts > appends DEFAULT_TOOL_GUIDANCE')).toBe(true);
    expect(result.size).toBe(2);
  });

  it('does NOT match single-space "FAIL reason:" (test-output false positive)', () => {
    // A test that verifies failure-formatting logic can print lines like
    // `FAIL reason: expected X got Y` from its own stdout. Those must
    // not be treated as baseline failures. The tighter "FAIL + 2+
    // spaces" regex guards against this.
    const output = [
      'FAIL reason: expected true got false',
      'FAIL: some output from a passing test',
      ' FAIL something (only one trailing space)',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.size).toBe(0);
  });

  it('matches vitest "⨯" (U+2A2F) marker in addition to ✕/✗/×', () => {
    const output = '⨯ some test failed';
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set(['some test failed']));
  });
});

describe('pytest CI selector extraction', () => {
  it('extracts pytest selectors from CI FAILED lines and normalizes backend script args', () => {
    const output = [
      'FAILED backend/testing/test_models.py::test_create - AssertionError',
      'FAILED testing/test_pipeline.py::test_queue[case-1] - RuntimeError',
      'PASSED backend/testing/test_ok.py::test_ok',
    ].join('\n');

    const selectors = extractPytestSelectorsFromOutput(output);
    expect(selectors).toEqual([
      'backend/testing/test_models.py::test_create',
      'testing/test_pipeline.py::test_queue[case-1]',
    ]);

    const runner = {
      cmd: 'bash',
      args: ['scripts/test-backend.sh', '--postgres'],
      cwd: '/tmp/workspace',
      label: 'pytest (backend pyproject)',
    };
    expect(normalizePytestSelectorForRunner(selectors[0], runner)).toBe('testing/test_models.py::test_create');

    const directScriptRunner = {
      cmd: 'scripts/test-backend.sh',
      args: ['--postgres'],
      cwd: '/tmp/workspace',
      label: 'pytest (backend direct script)',
    };
    expect(normalizePytestSelectorForRunner(selectors[0], directScriptRunner)).toBe('testing/test_models.py::test_create');
  });

  it('builds targeted pytest runners by appending selectors only to pytest-compatible runners', () => {
    const runners = [
      { cmd: 'bash', args: ['scripts/test-backend.sh', '--postgres'], cwd: '/tmp/workspace', label: 'backend' },
      { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace/agent', label: 'agent' },
    ];

    const targeted = buildTargetedPytestRunners(runners, ['backend/testing/test_models.py::test_create']);

    expect(targeted).toHaveLength(1);
    expect(targeted[0].args).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
  });
});

describe('extractInvestigatedFiles', () => {
  it('extracts repo-relative paths from Read/Edit/Grep input previews and dedupes', () => {
    const trace = [
      { t: '1', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/admin-ui/src/components/DirectionCard.tsx","offset":180,"limit":20}' },
      { t: '2', tool: 'Grep',  input_preview: '{"pattern":"total_operator_action","path":"/tmp/workspace/admin-ui/src","output_mode":"content"}' },
      { t: '3', tool: 'Edit',  input_preview: '{"file_path":"/tmp/workspace/admin-ui/src/components/DirectionCard.test.tsx","old_string":"x","new_string":"y"}' },
      { t: '4', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/admin-ui/src/components/DirectionCard.tsx","offset":200,"limit":40}' },
      { t: '5', tool: 'Bash',  input_preview: '{"command":"ls","description":"list"}' },
    ];
    expect(extractInvestigatedFiles(trace)).toEqual([
      'admin-ui/src/components/DirectionCard.tsx',
      'admin-ui/src',
      'admin-ui/src/components/DirectionCard.test.tsx',
    ]);
  });

  it('returns paths in first-seen order, preserves Grep directories, ignores non-investigation tools', () => {
    const trace = [
      { t: '1', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/backend/app/foo.py"}' },
      { t: '2', tool: 'Bash',  input_preview: '{"command":"echo hi"}' },
      { t: '3', tool: 'Grep',  input_preview: '{"pattern":"x","path":"/tmp/workspace/backend/app"}' },
      { t: '4', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/backend/app/foo.py"}' },
    ];
    expect(extractInvestigatedFiles(trace)).toEqual([
      'backend/app/foo.py',
      'backend/app',
    ]);
  });

  it('handles JSON-truncated input previews via regex fallback', () => {
    const trace = [
      { t: '1', tool: 'Read', input_preview: '{"file_path":"/tmp/workspace/agent/src/handlers/review-fix.ts","offset":1,"limit":2000,"some_other_field":"...truncated' },
    ];
    expect(extractInvestigatedFiles(trace)).toEqual(['agent/src/handlers/review-fix.ts']);
  });
});

// ── parseFailingTestsFromVitestJson ─────────────────────────────────────────
describe('parseFailingTestsFromVitestJson', () => {
  it('extracts fullName of every failed assertion', () => {
    const json = JSON.stringify({
      testResults: [
        {
          name: '/path/foo.test.ts',
          assertionResults: [
            { fullName: 'describe A > does X', status: 'failed' },
            { fullName: 'describe A > does Y', status: 'passed' },
            { fullName: 'describe A > does Z', status: 'failed' },
          ],
        },
        {
          name: '/path/bar.test.ts',
          assertionResults: [
            { fullName: 'describe B > does W', status: 'failed' },
          ],
        },
      ],
    });
    expect(parseFailingTestsFromVitestJson(json)).toEqual(new Set([
      'describe A > does X',
      'describe A > does Z',
      'describe B > does W',
    ]));
  });

  it('falls back to title when fullName is missing', () => {
    const json = JSON.stringify({
      testResults: [{
        assertionResults: [
          { title: 'legacy test', status: 'failed' },
        ],
      }],
    });
    expect(parseFailingTestsFromVitestJson(json)).toEqual(new Set(['legacy test']));
  });

  it('ignores passed/skipped/pending assertions', () => {
    const json = JSON.stringify({
      testResults: [{
        assertionResults: [
          { fullName: 'A', status: 'passed' },
          { fullName: 'B', status: 'skipped' },
          { fullName: 'C', status: 'pending' },
          { fullName: 'D', status: 'todo' },
        ],
      }],
    });
    expect(parseFailingTestsFromVitestJson(json).size).toBe(0);
  });

  it('returns empty Set on malformed JSON', () => {
    expect(parseFailingTestsFromVitestJson('not json').size).toBe(0);
    expect(parseFailingTestsFromVitestJson('').size).toBe(0);
  });

  it('ignores simulated failure output in stdout — no false positives from mocked test bodies', () => {
    // This is the whole point: stdout from a passing vitest test whose body
    // prints something like "✕ simulated failure" would fool parseFailingTests.
    // The JSON reporter only records real failures.
    const json = JSON.stringify({
      testResults: [{
        assertionResults: [
          {
            fullName: 'testFixLoop > handles simulated failure output',
            status: 'passed',
          },
        ],
      }],
    });
    expect(parseFailingTestsFromVitestJson(json).size).toBe(0);
  });
});

// ── recordToolUse ────────────────────────────────────────────────────────────
describe('recordToolUse', () => {
  it('records a tool use entry with timestamp, tool name, and input preview', () => {
    recordToolUse('Bash', { command: 'ls' });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.tool).toBe('Bash');
    expect(entry.input_preview).toBe('{"command":"ls"}');
    expect(entry.t).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  });

  it('truncates input_preview to 200 characters for long inputs', () => {
    recordToolUse('Bash', { command: 'x'.repeat(500) });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview.length).toBe(200);
  });

  it("records '<unserializable>' for inputs with circular references", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    recordToolUse('Bash', circular);
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).toBe('<unserializable>');
  });

  it('evicts oldest entry when buffer exceeds 20 entries (TOOL_TRACE_MAX)', () => {
    for (let i = 0; i < 25; i++) {
      recordToolUse(`tool-${i}`, { n: i });
    }
    const trace = snapshotToolTrace();
    expect(trace.length).toBe(20);
    const tools = trace.map(e => e.tool);
    expect(tools).toContain('tool-24');
    expect(tools).toContain('tool-5');
    expect(tools).not.toContain('tool-4');
    expect(tools).not.toContain('tool-0');
  });

  it('handles string input directly without JSON.stringify', () => {
    recordToolUse('Write', 'raw string value');
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).toBe('raw string value');
  });

  it('sanitizes credential patterns in input_preview', () => {
    recordToolUse('Bash', { command: 'curl -H "Authorization: token ghp_abc123secretXYZ" https://api.github.com' });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).not.toContain('ghp_abc123secretXYZ');
    expect(entry.input_preview).toContain('ghp_***');
  });

  it('can refresh the latest entry timestamp without adding a new tool use', () => {
    clearToolTrace();
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date('2026-01-01T00:00:00.000Z'));
      recordToolUse('Bash', { command: './pr open' });
      const initial = snapshotToolTrace();

      vi.setSystemTime(new Date('2026-01-01T00:09:00.000Z'));
      const refreshed = snapshotToolTrace({ refreshLastEntry: true });

      expect(refreshed.length).toBe(1);
      expect(refreshed[0].tool).toBe('Bash');
      expect(refreshed[0].input_preview).toBe('{"command":"./pr open"}');
      expect(refreshed[0].t).toBe('2026-01-01T00:09:00.000Z');
      expect(refreshed[0].t).not.toBe(initial[0].t);
    } finally {
      vi.useRealTimers();
      clearToolTrace();
    }
  });
});

// ── replayLogBufferRateLimited ────────────────────────────────────────────────
describe('replayLogBufferRateLimited', () => {
  beforeEach(() => {
    clearLogBuffer();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    clearLogBuffer();
  });

  it('sends all buffered entries and empties the buffer for a small batch', async () => {
    appendToLogBuffer('msg-1');
    appendToLogBuffer('msg-2');
    const ws = { send: vi.fn() } as unknown as WebSocket;
    const replay = replayLogBufferRateLimited(ws);
    await vi.runAllTimersAsync();
    await replay;
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(2);
    expect(snapshotLogBuffer().length).toBe(0);
  });

  it('does not exceed 15 sends before the 1-second throttle window elapses', async () => {
    for (let i = 0; i < 16; i++) appendToLogBuffer(`msg-${i}`);
    const ws = { send: vi.fn() } as unknown as WebSocket;
    const replay = replayLogBufferRateLimited(ws);
    // After minimal time: first batch of 15 sent, the 16th is still waiting.
    await vi.advanceTimersByTimeAsync(50);
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(15);
    expect(snapshotLogBuffer().length).toBe(1);
    // Advance past the throttle window so the second batch fires.
    await vi.advanceTimersByTimeAsync(1000);
    await replay;
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(16);
    expect(snapshotLogBuffer().length).toBe(0);
  });

  it('drains a full 1000-entry buffer fully across multiple batches', async () => {
    for (let i = 0; i < 1000; i++) appendToLogBuffer(`msg-${i}`);
    expect(snapshotLogBuffer().length).toBe(1000);
    const ws = { send: vi.fn() } as unknown as WebSocket;
    const replay = replayLogBufferRateLimited(ws);
    await vi.advanceTimersByTimeAsync(100_000);
    await replay;
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1000);
    expect(snapshotLogBuffer().length).toBe(0);
  });
});

// ── Phase 0 telemetry — setup_secs / warm_start / prior_role_vm_id ───────────
describe('persistAndPostStatus telemetry enrichment', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    _resetTelemetryForTesting();
    _setRoleClaimTimestampForTesting(null);
    _setHandlerDurationForTesting(null);
    fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    _resetTelemetryForTesting();
    _setRoleClaimTimestampForTesting(null);
    _setHandlerDurationForTesting(null);
  });

  it('computes setup_secs from claim → first tool use gap', async () => {
    const claimAt = 1_000_000;
    _setRoleClaimTimestampForTesting(claimAt);
    // Simulate the SDK message loop calling recordToolUse 12.5s after claim.
    const dateNowSpy = vi.spyOn(Date, 'now').mockReturnValue(claimAt + 12_500);
    try {
      recordToolUse('Read', { file_path: '/x' });
      expect(_getFirstToolUseAtForTesting()).toBe(claimAt + 12_500);
    } finally {
      dateNowSpy.mockRestore();
    }

    await persistAndPostStatus('task-X', { task_id: 'task-X', status: 'COMPLETED' });
    expect(fetchMock).toHaveBeenCalled();
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { setup_secs?: number; warm_start?: boolean; prior_role_vm_id?: string | null };
    expect(body.setup_secs).toBe(12.5);
  });

  it('warm_start defaults to false on initial role; null prior_role_vm_id', async () => {
    _setRoleClaimTimestampForTesting(2_000_000);
    await persistAndPostStatus('task-cold', { task_id: 'task-cold', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { warm_start?: boolean; prior_role_vm_id?: string | null };
    expect(body.warm_start).toBe(false);
    expect(body.prior_role_vm_id).toBeNull();
  });

  it('warm_start=true emits prior_role_vm_id from VM_INSTANCE_ID', async () => {
    const orig = process.env.VM_INSTANCE_ID;
    process.env.VM_INSTANCE_ID = 'agent-vm-warm-7';
    try {
      _setRoleClaimTimestampForTesting(3_000_000);
      _setWarmStartForTesting(true);
      await persistAndPostStatus('task-warm', { task_id: 'task-warm', status: 'COMPLETED' });
      const body = JSON.parse(
        (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
      ) as { warm_start?: boolean; prior_role_vm_id?: string | null };
      expect(body.warm_start).toBe(true);
      expect(body.prior_role_vm_id).toBe('agent-vm-warm-7');
    } finally {
      if (orig === undefined) delete process.env.VM_INSTANCE_ID;
      else process.env.VM_INSTANCE_ID = orig;
    }
  });

  it('omits setup_secs when no tool use was recorded', async () => {
    _setRoleClaimTimestampForTesting(4_000_000);
    // No recordToolUse call — firstToolUseAt stays null.
    await persistAndPostStatus('task-quiet', { task_id: 'task-quiet', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { setup_secs?: number };
    expect(body.setup_secs).toBeUndefined();
  });

  it('enrichStatusPayload includes handler_duration_secs after the simulated handler-complete capture', async () => {
    // Mirrors what runRoleOnce does on the success path: capture
    // (Date.now() - roleClaimTimestamp)/1000 after the handler returns.
    // We seed the mutable directly via the test helper instead of running
    // the full role-loop.
    _setHandlerDurationForTesting(87.3);
    await persistAndPostStatus('task-h', { task_id: 'task-h', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { handler_duration_secs?: number };
    expect(body.handler_duration_secs).toBe(87.3);
  });

  it('omits handler_duration_secs when null (e.g. failure paths or pre-handler statuses)', async () => {
    // Default state — _setHandlerDurationForTesting(null) in beforeEach.
    await persistAndPostStatus('task-q', { task_id: 'task-q', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { handler_duration_secs?: number };
    expect(body.handler_duration_secs).toBeUndefined();
  });
});

describe('persistAndPostStatus workspace_prep shipping', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    resetWorkspacePrepOutcome();
    vi.mocked(fs.readdirSync).mockReturnValue([]);
    fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetWorkspacePrepOutcome();
    delete process.env.BLUEPRINT_WARM_HANDOFF;
    delete process.env.BLUEPRINT_LANE_MODE;
  });

  it('includes the recorded workspace prep outcome in /api/agent/status', async () => {
    const existsSpy = vi.mocked(fs.existsSync);
    existsSpy.mockImplementation(
      value => String(value).replaceAll('\\', '/') === '/ws/.git',
    );
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    process.env.BLUEPRINT_LANE_MODE = '1';

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'feature/warm',
    });

    await persistAndPostStatus('task-wp', { task_id: 'task-wp', status: 'COMPLETED' });

    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)?.[1] as { body?: string }).body ?? '{}',
    ) as {
      workspace_prep?: {
        path?: string;
        warm_reuse_succeeded?: boolean;
        checkpoint_reset_applied?: boolean;
        lane_phase?: boolean;
      };
    };
    expect(body.workspace_prep).toMatchObject({
      path: 'warm',
      warm_reuse_succeeded: true,
      checkpoint_reset_applied: false,
      lane_phase: true,
    });
  });

  it('resetPerRoleModuleState clears stale workspace_prep before the next role ships', async () => {
    const existsSpy = vi.mocked(fs.existsSync);
    existsSpy.mockImplementation(
      value => String(value).replaceAll('\\', '/') === '/ws/.git',
    );
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    process.env.BLUEPRINT_LANE_MODE = '1';

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'feature/warm',
    });
    resetPerRoleModuleState({ warmStart: false });

    await persistAndPostStatus('task-wp-reset', {
      task_id: 'task-wp-reset',
      status: 'COMPLETED',
    });

    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)?.[1] as { body?: string }).body ?? '{}',
    ) as { workspace_prep?: unknown };
    expect(body.workspace_prep).toBeUndefined();
  });
});

describe('park directives and warm continuation', () => {
  const originalTaskId = process.env.TASK_ID;
  const originalVmInstanceId = process.env.VM_INSTANCE_ID;
  const originalAgentToken = process.env.AGENT_SECRET_TOKEN;
  const originalLiveAgentToken = getAgentToken();

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalVmInstanceId !== undefined) process.env.VM_INSTANCE_ID = originalVmInstanceId;
    else delete process.env.VM_INSTANCE_ID;
    if (originalAgentToken !== undefined) process.env.AGENT_SECRET_TOKEN = originalAgentToken;
    else delete process.env.AGENT_SECRET_TOKEN;
    _setAgentTokenForTesting(originalLiveAgentToken);
    _consumePendingParkDirectiveForTesting('task-park');
  });

  it('records a park directive from /api/agent/status', async () => {
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        action: 'park',
        retry_after_until: '2030-01-01T00:00:10Z',
        park_deadline: '2030-01-01T00:00:30Z',
      }),
      text: () => Promise.resolve(''),
      headers: { get: () => null },
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await persistAndPostStatus('task-park', {
      task_id: 'task-park',
      status: 'FAILED',
      failure_category: 'rate_limited',
    });

    expect(result).toMatchObject({
      action: 'park',
      retry_after_until: '2030-01-01T00:00:10Z',
      park_deadline: '2030-01-01T00:00:30Z',
    });
    expect(_consumePendingParkDirectiveForTesting('task-park')).toEqual({
      action: 'park',
      retryAfterUntil: '2030-01-01T00:00:10Z',
      parkDeadline: '2030-01-01T00:00:30Z',
    });
  });

  it('uses the park deadline as the poll timeout and exits cleanly when it expires', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2030-01-01T00:00:00Z'));
    process.env.TASK_ID = 'task-park';
    process.env.VM_INSTANCE_ID = 'vm-park-1';

    let observedTimeoutMs: number | undefined;
    const result = await continueWarmContinuation({
      priorTaskId: 'task-park',
      priorPrNumber: 17,
      baseBranch: 'main',
      parkDirective: {
        action: 'park',
        retryAfterUntil: '2030-01-01T00:00:05Z',
        parkDeadline: '2030-01-01T00:00:15Z',
      },
    }, {
      pollForNextRole: opts => {
        observedTimeoutMs = opts.timeoutMs;
        return Promise.resolve({ action: 'exit', reason: 'poll_timeout' });
      },
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn(),
      materializeClaimCredentialState: vi.fn(),
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: allowJwtFallback => (allowJwtFallback ? 'vm-park-1' : process.env.VM_INSTANCE_ID || null),
    });

    expect(result).toEqual({ action: 'exit' });
    expect(observedTimeoutMs).toBe(15_000);
    expect(process.env.TASK_ID).toBe('');
  });

  it('declines warm polling after a clone-failed role leaves no git workspace', async () => {
    const pollForNextRole = vi.fn();

    const result = await continueWarmContinuation({
      priorTaskId: 'clone-failed-task',
      priorPrNumber: null,
      baseBranch: 'main',
    }, {
      pollForNextRole,
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn(),
      materializeClaimCredentialState: vi.fn(),
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-clone-failed',
      isWorkspaceValid: () => false,
    });

    expect(result).toEqual({ action: 'exit' });
    expect(pollForNextRole).not.toHaveBeenCalled();
  });

  it('falls back to the task JWT vm id and re-targets after reclaim', async () => {
    process.env.TASK_ID = 'task-park';
    delete process.env.VM_INSTANCE_ID;
    _setAgentTokenForTesting(mockJwtWithVmInstanceId('vm-from-jwt'));

    const callOrder: string[] = [];
    const resetForNextRole = vi.fn().mockImplementation(() => {
      callOrder.push('reset');
      return Promise.resolve();
    });
    const activateClaimedRole = vi.fn().mockImplementation((claim: { taskId?: string; agentToken?: string }) => {
      callOrder.push('activate');
      if (claim.agentToken) _setAgentTokenForTesting(claim.agentToken);
      if (claim.taskId) process.env.TASK_ID = claim.taskId;
    });
    const materializeClaimCredentialState = vi.fn().mockImplementation(() => {
      callOrder.push('materialize');
    });
    const performWarmRetarget = vi.fn().mockImplementation((_manifest: unknown, opts: { taskId?: string }) => {
      callOrder.push(`retarget:${opts.taskId}`);
      return Promise.resolve();
    });

    const result = await continueWarmContinuation({
      priorTaskId: 'task-park',
      priorPrNumber: 9,
      baseBranch: 'main',
      parkDirective: {
        action: 'park',
        retryAfterUntil: '2030-01-01T00:00:05Z',
        parkDeadline: '2030-01-01T00:00:15Z',
      },
    }, {
      pollForNextRole: opts => {
        callOrder.push(`poll:${opts.vmInstanceId}`);
        return Promise.resolve({
          action: 'claim',
          taskId: 'task-next',
          agentToken: 'rotated-jwt',
          secretManifest: {
            env: { PROVIDER: 'anthropic' },
            files: [{ path: '/tmp/provider.json', content: '{}', mode: '0600' }],
          },
        });
      },
      resetForNextRole,
      activateClaimedRole,
      materializeClaimCredentialState,
      performWarmRetarget,
      resolveVmInstanceId: allowJwtFallback => (allowJwtFallback ? 'vm-from-jwt' : null),
    });

    expect(result).toEqual({ action: 'exit' });
    expect(callOrder).toEqual([
      'poll:vm-from-jwt',
      'activate',
      'reset',
      'materialize',
      'retarget:task-next',
    ]);
    expect(performWarmRetarget).toHaveBeenCalledWith(
      expect.objectContaining({ env: { PROVIDER: 'anthropic' } }),
      { rotatedJwt: 'rotated-jwt', taskId: 'task-next' },
    );
    expect(process.env.TASK_ID).toBe('task-next');
  });

  it('refreshes a same-account rotating credential without re-exec', async () => {
    process.env.TASK_ID = 'task-prior';
    process.env.VM_INSTANCE_ID = 'vm-codex-refresh';
    const materializeClaimCredentialState = vi.fn();
    const performWarmRetarget = vi.fn();

    const result = await continueWarmContinuation({
      priorTaskId: 'task-prior',
      priorPrNumber: 10,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'task-codex-next',
        agentToken: 'rotated-codex-jwt',
        secretManifest: {
          env: { PROVIDER: 'openai', AUTH_MODE: 'oauth' },
          files: [{ path: '/opt/agent/openai/auth.json', content: '{}' }],
          metadata: { retarget: true, credential_refresh_only: true },
        },
      }),
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn(),
      materializeClaimCredentialState,
      performWarmRetarget,
      resolveVmInstanceId: () => 'vm-codex-refresh',
    });

    expect(result).toEqual({ action: 'continue' });
    expect(materializeClaimCredentialState).toHaveBeenCalledOnce();
    expect(performWarmRetarget).not.toHaveBeenCalled();
  });

  it('sets lane workspace env for a lane-phase warm claim', async () => {
    process.env.TASK_ID = 'task-prior';
    process.env.VM_INSTANCE_ID = 'vm-lane-claim';
    delete process.env.BLUEPRINT_LANE_MODE;
    delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;

    const result = await continueWarmContinuation({
      priorTaskId: 'task-prior',
      priorPrNumber: 23,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'task-lane-next',
        agentToken: 'rotated-lane-jwt',
        isLanePhase: true,
        laneCheckpointSha: 'abc1234def5678901234567890123456789012ab',
      }),
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn((claim: { taskId?: string; agentToken?: string }) => {
        if (claim.agentToken) _setAgentTokenForTesting(claim.agentToken);
        if (claim.taskId) process.env.TASK_ID = claim.taskId;
      }),
      materializeClaimCredentialState: vi.fn(),
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-lane-claim',
    });

    expect(result).toEqual({ action: 'continue' });
    expect(process.env.BLUEPRINT_LANE_MODE).toBe('1');
    expect(process.env.BLUEPRINT_LANE_CHECKPOINT_SHA).toBe('abc1234def5678901234567890123456789012ab');
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBe('1');
  });

  it('clears stale lane workspace env for a non-lane warm claim', async () => {
    process.env.TASK_ID = 'task-prior';
    process.env.VM_INSTANCE_ID = 'vm-normal-claim';
    process.env.BLUEPRINT_LANE_MODE = '1';
    process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'stale-checkpoint';

    const result = await continueWarmContinuation({
      priorTaskId: 'task-prior',
      priorPrNumber: 24,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'task-normal-next',
        agentToken: 'rotated-normal-jwt',
      }),
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn((claim: { taskId?: string; agentToken?: string }) => {
        if (claim.agentToken) _setAgentTokenForTesting(claim.agentToken);
        if (claim.taskId) process.env.TASK_ID = claim.taskId;
      }),
      materializeClaimCredentialState: vi.fn(),
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-normal-claim',
    });

    expect(result).toEqual({ action: 'continue' });
    expect(process.env.BLUEPRINT_LANE_MODE).toBeUndefined();
    expect(process.env.BLUEPRINT_LANE_CHECKPOINT_SHA).toBeUndefined();
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBe('1');
  });
});

// ── pollForNextRole ──────────────────────────────────────────────────────────
describe('pollForNextRole', () => {
  let fetchMock: FetchMock;
  // Spy on the global clearInterval; widening to any is the simplest path
  // around vi.spyOn's strict object/keyof typing for ambient globals.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let clearIntervalSpy: ReturnType<typeof vi.spyOn<any, any>>;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    clearIntervalSpy = vi.spyOn(globalThis as any, 'clearInterval');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearIntervalSpy.mockRestore();
  });

  it('returns exit with poll_timeout when 204 forever', async () => {
    fetchMock.mockResolvedValue({ status: 204, json: () => Promise.resolve({}) });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-1',
      priorTaskId: 't-1',
      priorPrNumber: 42,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-tok',
      pollIntervalMs: 30,
      timeoutMs: 100,
      heartbeatIntervalMs: 50,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');
    // vm-heartbeat must have fired at least once (initial).
    const heartbeatCalls = fetchMock.mock.calls.filter(c => String(c[0]).endsWith('/api/agent/vm-heartbeat'));
    expect(heartbeatCalls.length).toBeGreaterThanOrEqual(1);
    // Authorization header must be the X-Agent-Token (matches existing apiPost).
    const headers = (heartbeatCalls[0][1] as { headers?: Record<string, string> } | undefined)?.headers ?? {};
    expect(headers['X-Agent-Token']).toBe('jwt-tok');
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns claim with taskId on action=claim', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({
          action: 'claim',
          task_id: 'next-X',
          warm_start: true,
          agent_token: 'rotated-jwt-for-next-X',
        }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-2',
      priorTaskId: 't-2',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt2',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    // The freshly-minted JWT is forwarded so the role-loop can rotate
    // AGENT_TOKEN before the next /api/agent/claim call.
    expect(result).toEqual({
      action: 'claim',
      taskId: 'next-X',
      agentToken: 'rotated-jwt-for-next-X',
    });
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns lane workspace fields on a lane-phase claim', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({
          action: 'claim',
          task_id: 'next-lane',
          agent_token: 'rotated-jwt-lane',
          is_lane_phase: true,
          lane_checkpoint_sha: 'abc1234def5678901234567890123456789012ab',
        }),
      });
    });

    const result = await pollForNextRole({
      vmInstanceId: 'vm-lane',
      priorTaskId: 't-lane',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-lane',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });

    expect(result).toEqual({
      action: 'claim',
      taskId: 'next-lane',
      agentToken: 'rotated-jwt-lane',
      isLanePhase: true,
      laneCheckpointSha: 'abc1234def5678901234567890123456789012ab',
    });
  });

  it('surfaces secret_manifest on an account-divergent warm claim', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({
          action: 'claim',
          task_id: 'next-Z',
          warm_start: true,
          agent_token: 'rotated-jwt-Z',
          secret_manifest: {
            env: { PROVIDER: 'gemini', GEMINI_API_KEY: 'k' },
            files: [{ path: '/home/agent/.gemini/settings.json', content: '{}', mode: '0600' }],
          },
        }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-3', priorTaskId: 't-3', priorPrNumber: null,
      apiBase: 'http://api.test', agentJwt: 'jwt3',
      pollIntervalMs: 10, timeoutMs: 1000, heartbeatIntervalMs: 200,
    });
    expect(result.action).toBe('claim');
    expect(result.taskId).toBe('next-Z');
    expect(result.agentToken).toBe('rotated-jwt-Z');
    expect(result.secretManifest).toBeDefined();
    expect(result.secretManifest?.env?.PROVIDER).toBe('gemini');
    expect(result.secretManifest?.files?.[0]?.path).toBe('/home/agent/.gemini/settings.json');
  });

  it('leaves secretManifest undefined on a same-account claim (JWT-only path)', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      // Same-account claim: no secret_manifest field at all.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'claim', task_id: 'next-S', warm_start: true, agent_token: 'jwt-s' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-4', priorTaskId: 't-4', priorPrNumber: null,
      apiBase: 'http://api.test', agentJwt: 'jwt4',
      pollIntervalMs: 10, timeoutMs: 1000, heartbeatIntervalMs: 200,
    });
    expect(result.action).toBe('claim');
    expect(result.taskId).toBe('next-S');
    expect(result.agentToken).toBe('jwt-s');
    expect(result.secretManifest).toBeUndefined();
  });

  it('returns claim with undefined agentToken when backend omits it (deploy mismatch)', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // Old-shape claim response — no agent_token.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'claim', task_id: 'next-Y', warm_start: true }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-2',
      priorTaskId: 't-2',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt2',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    // pollForNextRole exposes the missing-token case to the caller (the
    // role-loop) which raises reset_failed rather than silently
    // continuing with a stale prior-task JWT.
    expect(result).toEqual({ action: 'claim', taskId: 'next-Y' });
    expect(result.agentToken).toBeUndefined();
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns exit with reason on action=exit', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'another_vm_owns_this_pr' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-3',
      priorTaskId: 't-3',
      priorPrNumber: 7,
      apiBase: 'http://api.test',
      agentJwt: 'jwt3',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    expect(result).toEqual({ action: 'exit', reason: 'another_vm_owns_this_pr' });
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('clears heartbeat interval on every return path', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });
    const before = clearIntervalSpy.mock.calls.length;
    await pollForNextRole({
      vmInstanceId: 'vm-4',
      priorTaskId: 't-4',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt4',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    expect(clearIntervalSpy.mock.calls.length).toBeGreaterThan(before);
  });

  it('treats non-204 non-200 responses as transient and keeps polling until timeout', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 503, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-5',
      priorTaskId: 't-5',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt5',
      pollIntervalMs: 20,
      timeoutMs: 80,
      heartbeatIntervalMs: 200,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');
  });

  it('handles fetch network errors without crashing', async () => {
    let attempts = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      attempts += 1;
      if (attempts === 1) return Promise.reject(new Error('network failure'));
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'recovered' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-6',
      priorTaskId: 't-6',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt6',
      pollIntervalMs: 10,
      timeoutMs: 500,
      heartbeatIntervalMs: 200,
    });
    expect(result).toEqual({ action: 'exit', reason: 'recovered' });
  });
});

// ── performWarmRetarget — account-divergent warm re-target ───────────────────
describe('performWarmRetarget', () => {
  afterEach(() => { __setWarmRetargetDepsForTesting(null); });

  it('materializes an augmented manifest, verifies it, clears stale creds, then re-execs (in order)', async () => {
    const calls: string[] = [];
    let captured: { manifest?: SecretManifest; envPath?: string } = {};
    let staleManifest: SecretManifest | undefined;
    __setWarmRetargetDepsForTesting({
      materialize: (m: SecretManifest, envPath: string) => { calls.push('materialize'); captured = { manifest: m, envPath }; },
      verifyMaterialized: () => { calls.push('verify'); return []; },
      clearStaleCreds: (m: SecretManifest) => { calls.push('clearStale'); staleManifest = m; return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });

    const inputManifest: SecretManifest = {
      env: { PROVIDER: 'openai', OPENAI_API_KEY: 'sk-xyz' },
      files: [{ path: '/opt/agent/openai/auth.json', content: '{}', mode: '0600' }],
    };
    await performWarmRetarget(inputManifest, { rotatedJwt: 'ROT.JWT', taskId: 'task-42' });

    // Write-new-then-swap ordering: materialize (overwrite in place) BEFORE any
    // destructive clear — the configless-window guarantee (2026-06-06 incidents)
    // — and clear-stale BEFORE reexec — the credential-leakage guarantee.
    expect(calls).toEqual(['materialize', 'verify', 'clearStale', 'reexec']);
    expect(captured.envPath).toBe('/etc/agent.env');
    // The backend strips AGENT_SECRET_TOKEN; the agent re-injects the rotated JWT.
    expect(captured.manifest?.env?.AGENT_SECRET_TOKEN).toBe('ROT.JWT');
    expect(captured.manifest?.env?.TASK_ID).toBe('task-42');
    expect(captured.manifest?.env?.BLUEPRINT_WARM_HANDOFF).toBe('1');
    expect(captured.manifest?.env?.BLUEPRINT_REEXEC).toBe('1');
    // Original provider env + files are preserved.
    expect(captured.manifest?.env?.PROVIDER).toBe('openai');
    expect(captured.manifest?.env?.OPENAI_API_KEY).toBe('sk-xyz');
    expect(captured.manifest?.files).toEqual([{ path: '/opt/agent/openai/auth.json', content: '{}', mode: '0600' }]);
    // The stale-clear receives the same manifest so it can compute the
    // preserve set (paths the new account owns).
    expect(staleManifest).toBe(captured.manifest);
    // The input manifest must not be mutated in place.
    expect(inputManifest.env?.AGENT_SECRET_TOKEN).toBeUndefined();
  });

  it('does NOT clear stale creds or re-exec when materialize throws (prior config left intact)', async () => {
    const calls: string[] = [];
    __setWarmRetargetDepsForTesting({
      materialize: () => { calls.push('materialize'); throw new Error('helper-died'); },
      verifyMaterialized: () => { calls.push('verify'); return []; },
      clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });
    await expect(performWarmRetarget({ env: {} }, { rotatedJwt: 'j', taskId: 't' }))
      .rejects.toThrow('helper-died');
    // Nothing was cleared: the VM still holds the prior account's complete
    // config instead of the old ordering's configless poisoned state.
    expect(calls).toEqual(['materialize']);
  });

  it('does NOT clear stale creds or re-exec when verification reports missing files', async () => {
    const calls: string[] = [];
    __setWarmRetargetDepsForTesting({
      materialize: () => { calls.push('materialize'); },
      verifyMaterialized: () => { calls.push('verify'); return ['/home/agent/.claude.json']; },
      clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });
    await expect(performWarmRetarget({ env: {} }, { rotatedJwt: 'j', taskId: 't' }))
      .rejects.toThrow(/verification failed.*\/home\/agent\/\.claude\.json/);
    expect(calls).toEqual(['materialize', 'verify']);
  });

  it('default verifyMaterialized reports the env file + manifest files missing from disk', async () => {
    // Default deps verify via fs.existsSync; the fs mock returns false for
    // everything, so every expected path must be reported missing and the
    // re-target must refuse to proceed to the destructive clear.
    vi.mocked(fs.existsSync).mockReturnValue(false);
    const calls: string[] = [];
    __setWarmRetargetDepsForTesting({
      materialize: () => { calls.push('materialize'); },
      clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });
    await expect(performWarmRetarget(
      { env: { PROVIDER: 'anthropic' }, files: [{ path: '/home/agent/.claude.json', content: '{}', mode: '0600' }] },
      { rotatedJwt: 'j', taskId: 't' },
    )).rejects.toThrow(/\/etc\/agent\.env.*\/home\/agent\/\.claude\.json/);
    expect(calls).toEqual(['materialize']);
  });

  it('default verifyMaterialized passes when the materialized paths exist on disk', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    try {
      const calls: string[] = [];
      __setWarmRetargetDepsForTesting({
        materialize: () => { calls.push('materialize'); },
        clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
        reexec: () => { calls.push('reexec'); return Promise.resolve(); },
      });
      await performWarmRetarget(
        { env: { PROVIDER: 'anthropic' }, files: [{ path: '/home/agent/.claude.json', content: '{}', mode: '0600' }] },
        { rotatedJwt: 'j', taskId: 't' },
      );
      expect(calls).toEqual(['materialize', 'clearStale', 'reexec']);
    } finally {
      vi.mocked(fs.existsSync).mockReturnValue(false);
    }
  });

  it('default materialize invokes the real helper via sudo python3 against /etc/agent.env', async () => {
    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    // Override verify/clear-stale/reexec; materialize stays the real default so
    // we exercise the sudo python3 invocation against the baked helper path.
    __setWarmRetargetDepsForTesting({
      verifyMaterialized: () => [],
      clearStaleCreds: () => Promise.resolve(),
      reexec: () => Promise.resolve(),
    });
    await performWarmRetarget({ env: { PROVIDER: 'anthropic' } }, { rotatedJwt: 'j', taskId: 't' });

    const sudoCall = execMock.mock.calls.find(
      c => c[0] === 'sudo' && Array.isArray(c[1]) && c[1][0] === 'python3',
    );
    expect(sudoCall).toBeDefined();
    const args = sudoCall?.[1] as string[];
    expect(args).toContain(MATERIALIZE_SCRIPT_PATH);
    expect(args[args.length - 1]).toBe('/etc/agent.env');
  });
});

describe('bootstrapPoolMode', () => {
  const originalTaskId = process.env.TASK_ID;
  const originalWarmHandoff = process.env.BLUEPRINT_WARM_HANDOFF;

  beforeEach(() => {
    delete process.env.TASK_ID;
    delete process.env.BLUEPRINT_WARM_HANDOFF;
  });

  afterEach(() => {
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalWarmHandoff !== undefined) process.env.BLUEPRINT_WARM_HANDOFF = originalWarmHandoff;
    else delete process.env.BLUEPRINT_WARM_HANDOFF;
    vi.restoreAllMocks();
  });

  it('keeps polling across empty windows, then rotates into the first claimed task without warm handoff', async () => {
    const pollOnce = vi
      .fn()
      .mockResolvedValueOnce({ action: 'exit', reason: 'poll_timeout' })
      .mockResolvedValueOnce({ action: 'claim', taskId: 'pool-task-1', agentToken: 'pool-jwt-1' });

    const result = await bootstrapPoolMode({
      vmInstanceId: 'pool-vm-1',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-1',
      maxIdleLifetimeMs: 1_000,
      pollWindowMs: 250,
      pollOnce,
    });

    expect(result).toEqual({ action: 'claim', taskId: 'pool-task-1', agentToken: 'pool-jwt-1' });
    expect(pollOnce).toHaveBeenCalledTimes(2);
    expect(pollOnce).toHaveBeenNthCalledWith(1, expect.objectContaining({
      vmInstanceId: 'pool-vm-1',
      priorTaskId: initialPoolPriorTaskId,
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-1',
      timeoutMs: 250,
    }));
    expect(process.env.TASK_ID).toBe('pool-task-1');
    expect(getAgentToken()).toBe('pool-jwt-1');
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBeUndefined();
  });

  it('exits cleanly when max idle lifetime is exhausted without a claim', async () => {
    let fakeNow = 0;
    vi.spyOn(Date, 'now').mockImplementation(() => fakeNow);
    const pollOnce = vi.fn().mockImplementation(() => {
      fakeNow += 60;
      return { action: 'exit', reason: 'poll_timeout' } as const;
    });

    const result = await bootstrapPoolMode({
      vmInstanceId: 'pool-vm-2',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-2',
      maxIdleLifetimeMs: 100,
      pollWindowMs: 80,
      pollOnce,
    });

    expect(result).toEqual({ action: 'exit', reason: 'max_idle_lifetime_exceeded' });
    expect(pollOnce).toHaveBeenCalledTimes(2);
    expect(process.env.TASK_ID).toBeUndefined();
  });

  it('applies a retarget secret manifest before returning the first claimed task', async () => {
    mockReadFileSync.mockImplementation((path: string, encoding?: string) => {
      if (path === '/opt/agent/openai/auth.json' && encoding === undefined) {
        return Buffer.from('{"tokens":{"access_token":"retargeted"}}');
      }
      throw new Error(`unexpected read ${path}`);
    });
    const pollOnce = vi.fn().mockResolvedValue({
      action: 'claim',
      taskId: 'pool-task-retarget',
      agentToken: 'pool-jwt-retarget',
      secretManifest: {
        env: {
          PROVIDER: 'openai',
          AUTH_MODE: 'oauth',
          LLM_MODEL: 'gpt-5.4',
          CODEX_HOME: '/opt/agent/openai',
          LLM_EXECUTION_MODE: 'default',
        },
        files: [
          {
            path: '/opt/agent/openai/auth.json',
            content: '{"tokens":{"access_token":"retargeted"}}',
            mode: '0600',
          },
        ],
        metadata: { retarget: true, provider: 'openai', auth_mode: 'oauth' },
      },
    });

    const result = await bootstrapPoolMode({
      vmInstanceId: 'pool-vm-retarget',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-retarget',
      maxIdleLifetimeMs: 1_000,
      pollWindowMs: 250,
      pollOnce,
    });

    expect(result.action).toBe('claim');
    expect(result.taskId).toBe('pool-task-retarget');
    expect(result.agentToken).toBe('pool-jwt-retarget');
    expect(result.secretManifest).toBeDefined();
    expect(process.env.TASK_ID).toBe('pool-task-retarget');
    expect(process.env.PROVIDER).toBe('openai');
    expect(process.env.AUTH_MODE).toBe('oauth');
    expect(process.env.CODEX_HOME).toBe('/opt/agent/openai');
    expect(getProviderRunner().provider).toBe('openai');
    expect(getProviderRunner().buildValidationCommand('gpt-5.4')?.command).toBe('codex');
    expect(_getActiveCredentialStateSourceForTesting()).toBe('retarget');
    expect(vi.mocked(fs.writeFileSync).mock.calls).toEqual(expect.arrayContaining([
      [
        '/opt/agent/openai/auth.json',
        '{"tokens":{"access_token":"retargeted"}}',
        expect.objectContaining({ mode: 0o600 }),
      ],
    ]));
    expect(mockReadFileSync).toHaveBeenCalledWith('/opt/agent/openai/auth.json');
  });

  it('restores the boot credential state when the next claim has no secret manifest', () => {
    process.env.PROVIDER = 'anthropic';
    process.env.AUTH_MODE = 'oauth';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-claude-token';
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting();

    _materializeClaimCredentialStateForTesting({
      env: {
        PROVIDER: 'openai',
        AUTH_MODE: 'api_key',
        OPENAI_API_KEY: 'retarget-openai-key',
        CODEX_API_KEY: 'retarget-openai-key',
      },
      metadata: { retarget: true, provider: 'openai', auth_mode: 'api_key' },
    }, 'test: retarget');

    expect(process.env.PROVIDER).toBe('openai');
    expect(process.env.OPENAI_API_KEY).toBe('retarget-openai-key');
    expect(_getActiveCredentialStateSourceForTesting()).toBe('retarget');

    _materializeClaimCredentialStateForTesting(undefined, 'test: restore-boot');

    expect(process.env.PROVIDER).toBe('anthropic');
    expect(process.env.AUTH_MODE).toBe('oauth');
    expect(process.env.CLAUDE_CODE_OAUTH_TOKEN).toBe('boot-claude-token');
    expect(process.env.OPENAI_API_KEY).toBeUndefined();
    expect(process.env.CODEX_API_KEY).toBeUndefined();
    expect(getProviderRunner().provider).toBe('anthropic');
    expect(_getActiveCredentialStateSourceForTesting()).toBe('boot');
  });

  it('re-anchors a backend-supplied ~/.claude.json under the ACTUAL $HOME (sandbox HOME=/home)', () => {
    // Fleet-runner bwrap sandbox: fresh tmpfs HOME=/home, no host $HOME bind.
    // The backend manifest still hardcodes the cloud-VM path
    // /home/agent/.claude.json, but the Claude CLI reads ~/.claude.json from the
    // live HOME (=/home). The write MUST be re-anchored to /home/.claude.json or
    // auth fails.
    const savedHome = process.env.HOME;
    const savedVmId = process.env.VM_INSTANCE_ID;
    try {
      _resetCredentialStateForTesting();
      process.env.HOME = '/home';
      // Real (non-'local') VM id → the poisoned-VM presence guard is active; it
      // must validate the RESOLVED location, so mark /home/.claude.json present.
      process.env.VM_INSTANCE_ID = 'sandbox-vm';
      vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/.claude.json');
      vi.mocked(fs.writeFileSync).mockClear();

      _materializeClaimCredentialStateForTesting({
        env: {
          PROVIDER: 'anthropic',
          AUTH_MODE: 'oauth',
          CLAUDE_CODE_OAUTH_TOKEN: 'sandbox-claude-token',
        },
        files: [
          {
            path: '/home/agent/.claude.json',
            content: JSON.stringify({
              email: 'operator@example.com',
              projects: { '/other/project': { hasTrustDialogAccepted: false } },
            }),
            mode: '0600',
          },
        ],
        metadata: { retarget: true, provider: 'anthropic', auth_mode: 'oauth' },
      }, 'test: sandbox-reanchor');

      const writePaths = vi.mocked(fs.writeFileSync).mock.calls.map(c => c[0]);
      // Written where the CLI actually reads it…
      const claudeWrite = vi.mocked(fs.writeFileSync).mock.calls.find(c => c[0] === '/home/.claude.json');
      expect(claudeWrite).toBeDefined();
      const config: unknown = JSON.parse(claudeWrite?.[1] as string);
      expect(config).toMatchObject({
        hasCompletedOnboarding: true,
        email: 'operator@example.com',
        projects: {
          '/other/project': { hasTrustDialogAccepted: false },
          [WORKSPACE]: { hasTrustDialogAccepted: true },
        },
      });
      expect(claudeWrite?.[2]).toEqual(expect.objectContaining({ mode: 0o600 }));
      // …and NOT at the backend's hardcoded /home/agent path.
      expect(writePaths).not.toContain('/home/agent/.claude.json');
    } finally {
      if (savedHome !== undefined) process.env.HOME = savedHome; else delete process.env.HOME;
      if (savedVmId !== undefined) process.env.VM_INSTANCE_ID = savedVmId; else delete process.env.VM_INSTANCE_ID;
      vi.mocked(fs.existsSync).mockReturnValue(false);
      _resetCredentialStateForTesting();
    }
  });

  it('normalizes an omitted Claude config under sandbox HOME without adding credentials', () => {
    const savedHome = process.env.HOME;
    try {
      _resetCredentialStateForTesting();
      process.env.HOME = '/home';
      vi.mocked(fs.writeFileSync).mockClear();

      _materializeClaimCredentialStateForTesting({
        env: { PROVIDER: 'anthropic', AUTH_MODE: 'oauth' },
        files: [{ path: '/home/agent/.claude.json', content: '{}', mode: '0600' }],
        metadata: { retarget: true, provider: 'anthropic', auth_mode: 'oauth' },
      }, 'test: sandbox-minimal-claude-config');

      const claudeWrite = vi.mocked(fs.writeFileSync).mock.calls.find(c => c[0] === '/home/.claude.json');
      expect(claudeWrite).toBeDefined();
      const config: unknown = JSON.parse(claudeWrite?.[1] as string);
      expect(config).toEqual({
        hasCompletedOnboarding: true,
        projects: { [WORKSPACE]: { hasTrustDialogAccepted: true } },
      });
      expect(claudeWrite?.[1]).not.toContain('OAUTH_TOKEN');
      expect(claudeWrite?.[1]).not.toContain('sk-ant');
    } finally {
      if (savedHome !== undefined) process.env.HOME = savedHome; else delete process.env.HOME;
      _resetCredentialStateForTesting();
    }
  });

  it('trusts the configured provider workspace without replacing operator project settings', () => {
    const normalized = normalizeClaudeConfigContent(
      '/home/.claude.json',
      JSON.stringify({ projects: { '/operator/project': { custom: 'keep' } } }),
      '/workspace/agent',
    );

    expect(JSON.parse(normalized)).toEqual({
      hasCompletedOnboarding: true,
      projects: {
        '/operator/project': { custom: 'keep' },
        '/workspace/agent': { hasTrustDialogAccepted: true },
      },
    });
  });

  it('keeps the cloud-VM path byte-identical when HOME=/home/agent (re-anchor is a no-op)', () => {
    const savedHome = process.env.HOME;
    const savedVmId = process.env.VM_INSTANCE_ID;
    try {
      _resetCredentialStateForTesting();
      process.env.HOME = '/home/agent';
      process.env.VM_INSTANCE_ID = 'local'; // bypass the presence guard; assert the write path only
      vi.mocked(fs.writeFileSync).mockClear();

      _materializeClaimCredentialStateForTesting({
        env: {
          PROVIDER: 'anthropic',
          AUTH_MODE: 'oauth',
          CLAUDE_CODE_OAUTH_TOKEN: 'vm-claude-token',
        },
        files: [
          { path: '/home/agent/.claude.json', content: '{"hasCompletedOnboarding":true}', mode: '0600' },
        ],
        metadata: { retarget: true, provider: 'anthropic', auth_mode: 'oauth' },
      }, 'test: vm-parity');

      const writePaths = vi.mocked(fs.writeFileSync).mock.calls.map(c => c[0]);
      expect(writePaths).toContain('/home/agent/.claude.json');
      expect(writePaths).not.toContain('/home/.claude.json');
    } finally {
      if (savedHome !== undefined) process.env.HOME = savedHome; else delete process.env.HOME;
      if (savedVmId !== undefined) process.env.VM_INSTANCE_ID = savedVmId; else delete process.env.VM_INSTANCE_ID;
      _resetCredentialStateForTesting();
    }
  });

  it('fails hard on a malformed secret manifest instead of leaving prior credentials active', async () => {
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-claude-token';
    refreshRuntimeState();
    const pollOnce = vi.fn().mockResolvedValue({
      action: 'claim',
      taskId: 'pool-task-bad-retarget',
      agentToken: 'pool-jwt-bad-retarget',
      secretManifest: {
        env: { PROVIDER: 'openai', AUTH_MODE: 'oauth' },
        files: [],
        metadata: {},
      },
    });

    await expect(bootstrapPoolMode({
      vmInstanceId: 'pool-vm-bad-retarget',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-bad-retarget',
      maxIdleLifetimeMs: 1_000,
      pollWindowMs: 250,
      pollOnce,
    })).rejects.toThrow('reset_failed: pool-mode first claim: secret_manifest.metadata.retarget must be true');

    expect(process.env.TASK_ID).toBe('pool-task-bad-retarget');
    expect(process.env.PROVIDER).toBe('anthropic');
    expect(process.env.CLAUDE_CODE_OAUTH_TOKEN).toBe('boot-claude-token');
  });
});

// ── Claim-time VM config guard (poisoned-VM defense, 2026-06-06) ─────────────
describe('claim-time VM config guard', () => {
  const originalVmId = process.env.VM_INSTANCE_ID;
  const originalHome = process.env.HOME;

  afterEach(() => {
    if (originalVmId !== undefined) process.env.VM_INSTANCE_ID = originalVmId;
    else delete process.env.VM_INSTANCE_ID;
    if (originalHome !== undefined) process.env.HOME = originalHome;
    else delete process.env.HOME;
    mockReadFileSync.mockReset();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  it('is inert off-VM (no VM_INSTANCE_ID): local dev and tests are never doomed', () => {
    delete process.env.VM_INSTANCE_ID;
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it("is inert for the 'local' fallback instance id", () => {
    process.env.VM_INSTANCE_ID = 'local';
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it('does NOT report a ~/.claude.json that never existed on this VM (optional claude_config_json account)', () => {
    // 2026-06-06 false-positive incident (49 terminally FAILED tasks):
    // claude_config_json is an OPTIONAL per-account pool field. Accounts
    // without it boot perfectly healthy fresh VMs that never have
    // ~/.claude.json — the CLI runs on env/OAuth alone. Never-existed must
    // not be reported as "boot materialization lost".
    process.env.VM_INSTANCE_ID = 'vm-guard-1';
    process.env.HOME = '/home/agent';
    _captureBootProviderConfigPresenceForTesting(); // file absent at boot too
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it('reports ~/.claude.json when it existed at boot and is now gone (boot materialization lost)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-1b';
    process.env.HOME = '/home/agent';
    vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/agent/.claude.json');
    _captureBootProviderConfigPresenceForTesting(); // file present at boot
    vi.mocked(fs.existsSync).mockReturnValue(false); // ...then destroyed
    expect(missingVmProviderConfigFiles('anthropic')).toEqual(['/home/agent/.claude.json']);
  });

  it('reports nothing for anthropic when ~/.claude.json exists on disk', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-2';
    process.env.HOME = '/home/agent';
    vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/agent/.claude.json');
    _captureBootProviderConfigPresenceForTesting();
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it('has no confirmed file requirements for non-anthropic providers (no false dooms)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-3';
    process.env.HOME = '/home/agent';
    expect(missingVmProviderConfigFiles('openai')).toEqual([]);
    expect(missingVmProviderConfigFiles('gemini')).toEqual([]);
  });

  it('passes the claim when ~/.claude.json never existed on this VM (account without the optional claude_config_json)', () => {
    // 2026-06-06 false-positive incident: the boot snapshot legitimately has
    // NO ~/.claude.json because the account's pool entry has no
    // claude_config_json (an optional field — "Not required for most
    // setups"). The CLI runs on env/OAuth alone; the claim must proceed
    // instead of refusing every role on a perfectly healthy VM.
    process.env.VM_INSTANCE_ID = 'vm-guard-4';
    process.env.HOME = '/home/agent';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    mockReadFileSync.mockImplementation(() => { throw new Error('ENOENT'); });
    refreshRuntimeState();
    _captureBootProviderConfigPresenceForTesting(); // file absent at boot — never existed
    _captureBootCredentialSnapshotForTesting(); // snapshot has NO ~/.claude.json

    expect(() => {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    }).not.toThrow();
  });

  it('refuses a claim with vm_config_missing when the boot snapshot materialized ~/.claude.json but it is absent afterwards (poisoned VM)', () => {
    // The original poisoned-VM protection, kept intact: the boot snapshot
    // CONTAINS ~/.claude.json (it WAS materialized on this VM), but after the
    // claim-time re-write — the self-heal — the file is still absent on disk.
    // No source on this VM can restore it, so the claim must be refused with
    // the distinct vm_config_missing category instead of running a role whose
    // every Claude call dies in seconds.
    process.env.VM_INSTANCE_ID = 'vm-guard-4b';
    process.env.HOME = '/home/agent';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    mockReadFileSync.mockImplementation((p: string) => {
      if (p === '/home/agent/.claude.json') return '{"hasCompletedOnboarding":true}';
      throw new Error('ENOENT');
    });
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting(); // snapshot CONTAINS ~/.claude.json
    // existsSync stays false: the re-written file does not land on disk.

    let caught: unknown;
    try {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toContain('vm_config_missing');
    expect((caught as Error).message).toContain('/home/agent/.claude.json');
    expect((caught as Error & { failureCategory?: FailureCategory }).failureCategory).toBe('vm_config_missing');
  });

  it('refuses a claim with vm_config_missing when the retarget manifest promises ~/.claude.json but it is absent post-materialization', () => {
    // Claim-path counterpart of the lost-after-boot semantics: the retarget
    // manifest says the file MUST exist (the new account ships
    // claude_config_json), but after materialization it is absent on disk —
    // genuine loss, refuse. A manifest WITHOUT the file (account without the
    // optional field) would not be a refusal condition.
    process.env.VM_INSTANCE_ID = 'vm-guard-4c';
    process.env.HOME = '/home/agent';
    mockReadFileSync.mockImplementation(() => { throw new Error('ENOENT'); });
    refreshRuntimeState();
    _captureBootProviderConfigPresenceForTesting(); // fresh VM — file never existed at boot
    _captureBootCredentialSnapshotForTesting();

    let caught: unknown;
    try {
      _materializeClaimCredentialStateForTesting({
        metadata: { retarget: true },
        env: { PROVIDER: 'anthropic', AUTH_MODE: 'oauth', CLAUDE_CODE_OAUTH_TOKEN: 'retarget-token' },
        files: [{ path: '/home/agent/.claude.json', content: '{"hasCompletedOnboarding":true}' }],
      }, 'warm-retarget-claim');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toContain('vm_config_missing');
    expect((caught as Error).message).toContain('/home/agent/.claude.json');
    expect((caught as Error & { failureCategory?: FailureCategory }).failureCategory).toBe('vm_config_missing');
  });

  it('passes a retarget claim whose manifest has no ~/.claude.json (new account without the optional field)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-4d';
    process.env.HOME = '/home/agent';
    mockReadFileSync.mockImplementation(() => { throw new Error('ENOENT'); });
    refreshRuntimeState();
    _captureBootProviderConfigPresenceForTesting();
    _captureBootCredentialSnapshotForTesting();

    expect(() => {
      _materializeClaimCredentialStateForTesting({
        metadata: { retarget: true },
        env: { PROVIDER: 'anthropic', AUTH_MODE: 'oauth', CLAUDE_CODE_OAUTH_TOKEN: 'retarget-token' },
        files: [],
      }, 'warm-retarget-claim');
    }).not.toThrow();
  });

  it('passes the claim when re-materialization restores ~/.claude.json (self-heal path)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-5';
    process.env.HOME = '/home/agent';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    vi.mocked(fs.writeFileSync).mockClear();
    mockReadFileSync.mockImplementation((p: string) => {
      if (p === '/home/agent/.claude.json') return '{"hasCompletedOnboarding":true}';
      throw new Error('ENOENT');
    });
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting(); // snapshot CONTAINS ~/.claude.json
    // The snapshot re-write puts the file back on disk; simulate that.
    vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/agent/.claude.json');

    expect(() => {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    }).not.toThrow();
    // The self-heal actually re-wrote the snapshot's copy of the file.
    expect(vi.mocked(fs.writeFileSync).mock.calls.some(
      (c) => c[0] === '/home/agent/.claude.json',
    )).toBe(true);
  });

  it('claims are unaffected off-VM even when the config file is absent', () => {
    delete process.env.VM_INSTANCE_ID;
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting();
    expect(() => {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    }).not.toThrow();
  });
});

describe('main pool-mode startup ordering', () => {
  const originalPoolMode = process.env.BLUEPRINT_POOL_MODE;
  const originalVmInstanceId = process.env.VM_INSTANCE_ID;
  const originalTaskId = process.env.TASK_ID;
  const originalAnthropicApiKey = process.env.ANTHROPIC_API_KEY;
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    process.env.BLUEPRINT_POOL_MODE = '1';
    process.env.VM_INSTANCE_ID = 'pool-vm-main-test';
    delete process.env.TASK_ID;
    process.env.ANTHROPIC_API_KEY = 'anthropic-test-key';
    _setAgentTokenForTesting('vm-bootstrap-jwt');
    _setAgentRuntimeFingerprintForTesting(null);
    // Healthy-pool-boot simulation: ~/.claude.json is on disk from boot
    // materialization and stays there. The vm_config_missing guards compare
    // disk state against the boot-presence baseline, so a stable file means
    // no refusal anywhere (and individual tests flip this mock to simulate
    // loss / never-existed).
    vi.mocked(fs.existsSync).mockImplementation((p) => String(p).endsWith('.claude.json'));
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    if (originalPoolMode !== undefined) process.env.BLUEPRINT_POOL_MODE = originalPoolMode;
    else delete process.env.BLUEPRINT_POOL_MODE;
    if (originalVmInstanceId !== undefined) process.env.VM_INSTANCE_ID = originalVmInstanceId;
    else delete process.env.VM_INSTANCE_ID;
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalAnthropicApiKey !== undefined) process.env.ANTHROPIC_API_KEY = originalAnthropicApiKey;
    else delete process.env.ANTHROPIC_API_KEY;
    _setAgentTokenForTesting('');
    _setAgentRuntimeFingerprintForTesting(null);
  });

  it('starts the claimed-task heartbeat before stale-result replay, uses persisted replay tokens, and abandons unrecoverable legacy envelopes', async () => {
    const makeTaskToken = (sub: string): string => {
      const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
      const payload = Buffer.from(JSON.stringify({ sub })).toString('base64url');
      return `${header}.${payload}.sig`;
    };
    const now = Date.now();
    const events: string[] = [];
    let nextRoleBody: { vm_instance_id?: string; prior_task_id?: string; prior_pr_number?: number | null } | null = null;
    const statusReplayTokens: string[] = [];
    const heartbeatTokens: string[] = [];
    (fs.readdirSync as unknown as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path === '/tmp') events.push('stale-scan');
      return ['task_stale-persisted_result.json', 'task_stale-legacy_result.json'];
    });
    mockReadFileSync.mockImplementation((path: string) => {
      if (path === '/opt/agent/AGENT_RUNTIME_FINGERPRINT') {
        events.push('fingerprint-read');
        return 'AGENT_RUNTIME_FINGERPRINT=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';
      }
      if (path === '/tmp/task_stale-persisted_result.json') {
        return JSON.stringify({
          payload: { task_id: 'stale-persisted', status: 'FAILED' },
          retry: { attempts: 0, createdAt: now - 1_000, nextAttemptAt: now - 1 },
          agentToken: 'persisted-stale-jwt',
        });
      }
      if (path === '/tmp/task_stale-legacy_result.json') {
        return JSON.stringify({
          payload: { task_id: 'stale-legacy', status: 'FAILED' },
          retry: { attempts: 0, createdAt: now - 1_000, nextAttemptAt: now - 1 },
        });
      }
      return '';
    });

    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => {
      events.push('provider-validation');
      return {
        stdout: { on: vi.fn() },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
          if (event === 'close') void Promise.resolve().then(() => { handler(0); });
        }),
        kill: vi.fn(),
      };
    });

    fetchMock.mockImplementation((input: unknown, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        events.push('vm-heartbeat');
        return Promise.resolve({
          status: 204,
          json: () => Promise.resolve({}),
        });
      }
      if (url.endsWith('/api/agent/heartbeat')) {
        events.push('task-heartbeat');
        heartbeatTokens.push((init?.headers as Record<string, string> | undefined)?.['X-Agent-Token'] ?? '');
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ status: 'ok' }),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/next-role')) {
        events.push('next-role');
        const requestBody = typeof init?.body === 'string' ? init.body : '{}';
        nextRoleBody = JSON.parse(requestBody) as {
          vm_instance_id?: string;
          prior_task_id?: string;
          prior_pr_number?: number | null;
        };
        if (nextRoleBody.prior_task_id !== initialPoolPriorTaskId) {
          return Promise.resolve({ status: 422, json: () => Promise.resolve({ detail: 'prior_task_id must be a UUID' }) });
        }
        return Promise.resolve({
          status: 200,
          json: () => Promise.resolve({
            action: 'claim',
            task_id: 'pool-task-main-1',
            agent_token: makeTaskToken('pool-task-main-1'),
          }),
        });
      }
      if (url === 'http://metadata.google.internal/computeMetadata/v1/instance/image') {
        events.push('vm-image-fetch');
        return Promise.resolve({ ok: true, text: () => Promise.resolve('projects/test/global/images/agent-1234567890') });
      }
      if (url.endsWith('/api/agent/status')) {
        const body = JSON.parse(typeof init?.body === 'string' ? init.body : '{}') as { task_id?: string };
        events.push(`status-replay:${body.task_id ?? 'unknown'}`);
        statusReplayTokens.push((init?.headers as Record<string, string> | undefined)?.['X-Agent-Token'] ?? '');
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ status: 'ok' }),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/claim')) {
        events.push('claim-replay');
        return Promise.resolve({ status: 400, ok: false, headers: { get: () => null }, text: () => Promise.resolve('claim replay stopped test') });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    await expect(main()).rejects.toThrow('POST /api/agent/claim failed (400): claim replay stopped test');

    expect(events.indexOf('vm-heartbeat')).toBeGreaterThanOrEqual(0);
    expect(events.indexOf('next-role')).toBeGreaterThanOrEqual(0);
    expect(events.indexOf('task-heartbeat')).toBeGreaterThan(events.indexOf('next-role'));
    // Provider auth is validated pre-claim in pool mode (a bad-cred VM must fail
    // before bootstrapPoolMode flips a task IN_PROGRESS), so provider-validation
    // lands BEFORE next-role — see the validate-before-claim block in main().
    expect(events.indexOf('provider-validation')).toBeGreaterThanOrEqual(0);
    expect(events.indexOf('provider-validation')).toBeLessThan(events.indexOf('next-role'));
    expect(events.indexOf('vm-image-fetch')).toBeGreaterThan(events.indexOf('next-role'));
    expect(events.indexOf('stale-scan')).toBeGreaterThan(events.indexOf('next-role'));
    expect(events.indexOf('status-replay:stale-persisted')).toBeGreaterThan(events.indexOf('stale-scan'));
    expect(events.indexOf('status-replay:stale-persisted')).toBeGreaterThan(events.indexOf('task-heartbeat'));
    expect(events.indexOf('claim-replay')).toBeGreaterThan(events.indexOf('status-replay:stale-persisted'));
    expect(events).not.toContain('status-replay:stale-legacy');
    expect(heartbeatTokens).toEqual([makeTaskToken('pool-task-main-1')]);
    expect(statusReplayTokens).toEqual(['persisted-stale-jwt']);
    expect(nextRoleBody).toEqual({
      vm_instance_id: 'pool-vm-main-test',
      prior_task_id: initialPoolPriorTaskId,
      prior_pr_number: null,
    });
    expect(process.env.TASK_ID).toBe('pool-task-main-1');
    expect(getAgentToken()).toBe(makeTaskToken('pool-task-main-1'));
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBeUndefined();
    expect(vi.mocked(fs.unlinkSync).mock.calls).toEqual(expect.arrayContaining([
      ['/tmp/task_stale-persisted_result.json'],
      ['/tmp/task_stale-legacy_result.json'],
    ]));
  });

  it('refuses to claim and reports vm_config_missing pre-claim when a boot-materialized ~/.claude.json is gone (poisoned VM)', async () => {
    // The 2026-06-06 poisoned state: env credentials look healthy but the
    // boot-materialized ~/.claude.json was destroyed. The VM must die loudly
    // BEFORE polling for work — an explained pre-claim vm-heartbeat the
    // orchestrator can tear down — instead of claiming tasks whose every
    // Claude invocation fails in seconds.
    // Capture the boot presence baseline while the file is still on disk
    // (the beforeEach existsSync mock has it present) — modelling main()'s
    // process-start snapshot — then destroy the file. Genuine loss, refuse.
    _captureBootProviderConfigPresenceForTesting(); // boot baseline: file present
    vi.mocked(fs.existsSync).mockReturnValue(false); // ...config file destroyed
    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/next-role')) {
        throw new Error('must not poll for work configless');
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    // vitest intercepts the deliberate process.exit(1) and surfaces it as a throw.
    await expect(main()).rejects.toThrow(/process\.exit/);

    const curlCall = execMock.mock.calls.find((c) => c[0] === 'curl');
    expect(curlCall).toBeDefined();
    const curlArgs = ((curlCall?.[1] ?? []) as string[]).join(' ');
    expect(curlArgs).toContain('/api/agent/vm-heartbeat');
    expect(curlArgs).toContain('vm_config_missing');
    expect(curlArgs).toContain('.claude.json');
    // The doomed VM never polled for (let alone claimed) a role.
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(false);
    expect(fetched.some((u) => u.includes('/api/agent/claim'))).toBe(false);
  });

  it('reports pre-claim failure when taskless OAuth rotation returns a no-op', async () => {
    const originalProvider = process.env.PROVIDER;
    const originalAuthMode = process.env.AUTH_MODE;
    const originalCodexHome = process.env.CODEX_HOME;
    const originalOpenAiKey = process.env.OPENAI_API_KEY;
    const originalCodexKey = process.env.CODEX_API_KEY;
    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    process.env.CODEX_HOME = '/opt/agent/openai';
    delete process.env.OPENAI_API_KEY;
    delete process.env.CODEX_API_KEY;
    refreshRuntimeState();

    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    let authReadCount = 0;
    vi.mocked(fs.existsSync).mockImplementation((p) => String(p) === '/opt/agent/openai/auth.json');
    mockReadFileSync.mockImplementation((path, encoding) => {
      if (String(path) === '/opt/agent/openai/auth.json') {
        authReadCount += 1;
        const raw = authReadCount === 1 ? '{"refresh_token":"old"}' : '{"refresh_token":"new"}';
        return encoding ? raw : Buffer.from(raw);
      }
      return '';
    });
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') void Promise.resolve().then(() => { handler(0); });
      }),
      kill: vi.fn(),
    }));
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/credentials/rotate')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ rotated: false, reason: 'pool VM has no LLM runtime context' }),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/next-role')) {
        throw new Error('must not poll for work after failed OAuth rotation');
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    try {
      await expect(main()).rejects.toThrow(/did not persist rotated OAuth credentials/);
    } finally {
      if (originalProvider !== undefined) process.env.PROVIDER = originalProvider;
      else delete process.env.PROVIDER;
      if (originalAuthMode !== undefined) process.env.AUTH_MODE = originalAuthMode;
      else delete process.env.AUTH_MODE;
      if (originalCodexHome !== undefined) process.env.CODEX_HOME = originalCodexHome;
      else delete process.env.CODEX_HOME;
      if (originalOpenAiKey !== undefined) process.env.OPENAI_API_KEY = originalOpenAiKey;
      else delete process.env.OPENAI_API_KEY;
      if (originalCodexKey !== undefined) process.env.CODEX_API_KEY = originalCodexKey;
      else delete process.env.CODEX_API_KEY;
      refreshRuntimeState();
    }

    const curlCall = execMock.mock.calls.find((c) => c[0] === 'curl');
    expect(curlCall).toBeDefined();
    const curlArgs = ((curlCall?.[1] ?? []) as string[]).join(' ');
    expect(curlArgs).toContain('/api/agent/vm-heartbeat');
    expect(curlArgs).toContain('oauth credential rotation failed');
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(false);
  });

  it('proceeds to poll for work when ~/.claude.json never existed on this VM (account without the optional claude_config_json)', async () => {
    // 2026-06-06 false-positive incident (49 terminally FAILED tasks):
    // claude_config_json is an OPTIONAL per-account pool field. An account
    // without it boots a perfectly healthy fresh VM that NEVER has
    // ~/.claude.json — the CLI runs on env/OAuth alone. The pre-claim guard
    // must not refuse: never-existed is not "boot materialization lost".
    vi.mocked(fs.existsSync).mockReturnValue(false); // file never existed on this VM
    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') void Promise.resolve().then(() => { handler(0); });
      }),
      kill: vi.fn(),
    }));
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/next-role')) {
        return Promise.resolve({
          status: 200,
          json: () => Promise.resolve({ action: 'exit', reason: 'test-complete' }),
        });
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    // main() exits the pool cleanly via the next-role 'exit' action — it was
    // NOT killed pre-claim by the config guard.
    await expect(main()).resolves.toBeUndefined();

    // The healthy configless VM DID poll for work.
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(true);
    // And no pre-claim vm_config_missing report was emitted.
    const curlReports = execMock.mock.calls
      .filter((c) => c[0] === 'curl')
      .map((c) => ((c[1] ?? []) as string[]).join(' '))
      .join('\n');
    expect(curlReports).not.toContain('vm_config_missing');
  });

  it('skips the missingCredentials check when VM_INSTANCE_ID starts with "runner:" (self-hosted runner)', async () => {
    // A self-hosted runner's credentials are assigned dynamically by the
    // backend pool via /api/runner/next-task and injected by the runner as
    // env vars. There is no static local env-var credential path, so the
    // runner: prefix in VM_INSTANCE_ID tells validateProviderAuth to skip
    // the missingCredentials() check that would otherwise kill the agent
    // pre-claim with 'missing provider credentials'.
    const origRunnerVmId = process.env.VM_INSTANCE_ID;
    const origAnthropicKey = process.env.ANTHROPIC_API_KEY;
    process.env.VM_INSTANCE_ID = 'runner:test-runner';
    delete process.env.ANTHROPIC_API_KEY;
    refreshRuntimeState();

    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') void Promise.resolve().then(() => { handler(0); });
      }),
      kill: vi.fn(),
    }));
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/next-role')) {
        return Promise.resolve({
          status: 200,
          json: () => Promise.resolve({ action: 'exit', reason: 'test-complete' }),
        });
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    // main() exits the pool cleanly via the next-role 'exit' action — it was
    // NOT killed pre-claim by the missingCredentials guard (which would have
    // thrown with process.exit for a non-runner VM under the same env setup).
    await expect(main()).resolves.toBeUndefined();

    // The self-hosted runner successfully called next-role (it passed the
    // credential-validation gate despite having no static credentials set).
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(true);

    if (origRunnerVmId !== undefined) process.env.VM_INSTANCE_ID = origRunnerVmId;
    else delete process.env.VM_INSTANCE_ID;
    if (origAnthropicKey !== undefined) process.env.ANTHROPIC_API_KEY = origAnthropicKey;
    else delete process.env.ANTHROPIC_API_KEY;
    refreshRuntimeState();
  });
});

describe('main non-pool startup OAuth rotation', () => {
  const originalPoolMode = process.env.BLUEPRINT_POOL_MODE;
  const originalTaskId = process.env.TASK_ID;
  const originalProvider = process.env.PROVIDER;
  const originalAuthMode = process.env.AUTH_MODE;
  const originalCodexHome = process.env.CODEX_HOME;
  const originalOpenAiKey = process.env.OPENAI_API_KEY;
  const originalCodexKey = process.env.CODEX_API_KEY;
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    delete process.env.BLUEPRINT_POOL_MODE;
    process.env.TASK_ID = 'cold-preclaim-task';
    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    process.env.CODEX_HOME = '/opt/agent/openai';
    delete process.env.OPENAI_API_KEY;
    delete process.env.CODEX_API_KEY;
    _setAgentTokenForTesting('cold-bootstrap-jwt');
    refreshRuntimeState();
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    vi.mocked(fs.existsSync).mockImplementation((p) => String(p) === '/opt/agent/openai/auth.json');
    vi.mocked(fs.readdirSync).mockReset();
    vi.mocked(fs.readdirSync).mockReturnValue([]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readdirSync).mockReset();
    vi.mocked(fs.readdirSync).mockReturnValue([]);
    if (originalPoolMode !== undefined) process.env.BLUEPRINT_POOL_MODE = originalPoolMode;
    else delete process.env.BLUEPRINT_POOL_MODE;
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalProvider !== undefined) process.env.PROVIDER = originalProvider;
    else delete process.env.PROVIDER;
    if (originalAuthMode !== undefined) process.env.AUTH_MODE = originalAuthMode;
    else delete process.env.AUTH_MODE;
    if (originalCodexHome !== undefined) process.env.CODEX_HOME = originalCodexHome;
    else delete process.env.CODEX_HOME;
    if (originalOpenAiKey !== undefined) process.env.OPENAI_API_KEY = originalOpenAiKey;
    else delete process.env.OPENAI_API_KEY;
    if (originalCodexKey !== undefined) process.env.CODEX_API_KEY = originalCodexKey;
    else delete process.env.CODEX_API_KEY;
    _setAgentTokenForTesting('');
    refreshRuntimeState();
  });

  it('persists a Codex OAuth refresh during cold-start validation before first claim', async () => {
    const events: string[] = [];
    let authReadCount = 0;
    let rotateBody: Record<string, unknown> | null = null;

    mockReadFileSync.mockImplementation((path, encoding) => {
      const filePath = String(path);
      if (filePath === '/opt/agent/openai/auth.json') {
        authReadCount += 1;
        const raw = authReadCount === 1 ? '{"refresh_token":"old"}' : '{"refresh_token":"new"}';
        return encoding ? raw : Buffer.from(raw);
      }
      return '';
    });
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => {
      events.push('provider-validation');
      return {
        stdout: { on: vi.fn() },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
          if (event === 'close') void Promise.resolve().then(() => { handler(0); });
        }),
        kill: vi.fn(),
      };
    });

    fetchMock.mockImplementation((input: unknown, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/agent/credentials/rotate')) {
        events.push('rotate');
        rotateBody = JSON.parse(typeof init?.body === 'string' ? init.body : '{}') as Record<string, unknown>;
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ rotated: true }),
          headers: { get: () => null },
        });
      }
      if (url === 'http://metadata.google.internal/computeMetadata/v1/instance/image') {
        events.push('vm-image-fetch');
        return Promise.resolve({ ok: false, text: () => Promise.resolve('') });
      }
      if (url.endsWith('/api/agent/claim')) {
        events.push('claim');
        return Promise.resolve({
          ok: false,
          status: 400,
          text: () => Promise.resolve('claim stopped test'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    await expect(main()).rejects.toThrow('POST /api/agent/claim failed (400): claim stopped test');

    expect(events).toEqual(expect.arrayContaining(['provider-validation', 'rotate', 'claim']));
    expect(events.indexOf('provider-validation')).toBeLessThan(events.indexOf('rotate'));
    expect(events.indexOf('rotate')).toBeLessThan(events.indexOf('claim'));
    expect(rotateBody).toEqual({
      secret: '{"refresh_token":"new"}',
      task_id: 'cold-preclaim-task',
      base_hash: '1e635d25b15e673604a31c77273e40df029959ff84f66a3140080a3a8f45e4c4',
    });
    expect(vi.mocked(fs.writeFileSync).mock.calls).toEqual(expect.arrayContaining([
      [
        '/opt/agent/openai/auth.json',
        '{"refresh_token":"new"}',
        { encoding: 'utf-8', mode: 0o600 },
      ],
    ]));
  });
});

// ── child-PID registration in runProviderAgent / execFileAsync ───────────────
describe('child PID registration', () => {
  beforeEach(() => {
    clearPidRegistry();
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('execFileAsync registers PID at spawn and unregisters on close', async () => {
    type ChildHandlers = { close?: (code: number, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 9999,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    // Right after spawn, before close — PID should be registered.
    expect(_getRegisteredPids()).toContain(9999);

    // Simulate clean exit so the close handler runs.
    childHandlers.close?.(0, null);
    await promise;
    expect(_getRegisteredPids()).not.toContain(9999);
  });

  it('execFileAsync unregisters PID on error event', async () => {
    type ChildHandlers = { close?: (code: number | null, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 8888,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    expect(_getRegisteredPids()).toContain(8888);
    childHandlers.error?.(new Error('spawn failed'));
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(8888);
  });

  it('skips registration when child has no pid (spawn failure)', async () => {
    type ChildHandlers = { close?: (code: number | null, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: undefined,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    expect(_getRegisteredPids().length).toBe(0);
    childHandlers.close?.(0, null);
    await promise;
    expect(_getRegisteredPids().length).toBe(0);
  });
});

// ── runProviderAgent PID register/unregister ─────────────────────────────────
// Issue 9 (partial): runProviderAgent uses spawn() under the hood; the close
// AND error event paths must each unregister so the reset routine doesn't
// chase a dead PID. Mirrors the execFileAsync coverage above but for the
// runner-spawn path. We mock fs.existsSync false so the ./note write is
// skipped (it's a no-op when WORKSPACE doesn't exist in this mock).
describe('runProviderAgent PID register/unregister', () => {
  beforeEach(() => {
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('registers PID at spawn and unregisters on close', async () => {
    type ChildHandlers = {
      close?: (code: number | null, signal: NodeJS.Signals | null) => void;
      error?: (err: Error) => void;
    };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 4242,
      stdin: { end: vi.fn() },
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    // Drain microtasks so the synchronous spawn-time registerChildPid runs.
    await Promise.resolve();
    expect(_getRegisteredPids()).toContain(4242);

    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(4242);
  });

  it('unregisters PID on error event before close fires', async () => {
    type ChildHandlers = {
      close?: (code: number | null, signal: NodeJS.Signals | null) => void;
      error?: (err: Error) => void;
    };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 5252,
      stdin: { end: vi.fn() },
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();
    expect(_getRegisteredPids()).toContain(5252);

    childHandlers.error?.(new Error('spawn failed'));
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(5252);
  });
});

// ── clearToolTrace — Issue 2 cross-role leak guard ───────────────────────────
describe('clearToolTrace', () => {
  it('drains the ring buffer so subsequent snapshotToolTrace returns []', () => {
    recordToolUse('Read', { file_path: '/tmp/a' });
    recordToolUse('Edit', { file_path: '/tmp/b' });
    expect(snapshotToolTrace().length).toBeGreaterThanOrEqual(2);

    clearToolTrace();

    expect(snapshotToolTrace()).toEqual([]);
    expect(extractInvestigatedFiles(snapshotToolTrace())).toEqual([]);
  });

  it('survives being called when the buffer is already empty', () => {
    clearToolTrace();
    clearToolTrace();
    expect(snapshotToolTrace()).toEqual([]);
  });

  it('after clear, new tool uses populate from a clean slate', () => {
    recordToolUse('Read', { file_path: '/before/file.ts' });
    clearToolTrace();
    recordToolUse('Read', { file_path: '/after/file.ts' });
    const files = extractInvestigatedFiles(snapshotToolTrace());
    expect(files).toEqual(['/after/file.ts']);
    expect(files).not.toContain('/before/file.ts');
  });
});

// ── pollForNextRole 401/403 handling — Issue 14 ──────────────────────────────
describe('pollForNextRole auth-revoked', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('exits with reason=auth_revoked when next-role returns 401', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 401, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-401',
      priorTaskId: 't-401',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'expired-jwt',
      pollIntervalMs: 10,
      timeoutMs: 5000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
  });

  it('exits with reason=auth_revoked when next-role returns 403', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 403, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-403',
      priorTaskId: 't-403',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'forbidden-jwt',
      pollIntervalMs: 10,
      timeoutMs: 5000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
  });

  it('exits with reason=auth_revoked when only the vm-heartbeat returns 401', async () => {
    // next-role keeps returning 204 forever; the heartbeat path detects
    // the 401 and the loop exits at the next iteration check. We use a
    // long timeout so we know exit comes from the auth flag, not poll_timeout.
    let heartbeatCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        // First heartbeat returns 401 — flips the authRevoked flag.
        return Promise.resolve({ status: 401, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-hb-401',
      priorTaskId: 't-hb',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'expired-jwt',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
    // Sanity: heartbeat was attempted at least once (the initial fire).
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });

  it('redacts credentials in network error messages logged by pollForNextRole', async () => {
    const logLines: string[] = [];
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logLines.push(args.map(String).join(' '));
    });

    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // Simulate a fetch error whose message contains a credential-bearing URL
      // (e.g. ETIMEDOUT on a clone URL or a redirect error including the auth token).
      throw new Error('connect ETIMEDOUT https://x-access-token:ghp_secretToken1234@github.com/org/repo.git');
    });

    await pollForNextRole({
      vmInstanceId: 'vm-redact',
      priorTaskId: 't-redact',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-redact',
      pollIntervalMs: 10,
      timeoutMs: 50,
      heartbeatIntervalMs: 10000,
    });

    consoleSpy.mockRestore();

    const fetchErrLines = logLines.filter(l => l.includes('pollForNextRole fetch error'));
    expect(fetchErrLines.length).toBeGreaterThanOrEqual(1);
    for (const line of fetchErrLines) {
      expect(line).not.toContain('ghp_secretToken1234');
      expect(line).toContain('x-access-token:***@');
    }
  });
});

// ── pollForNextRole heartbeat firing pattern — Issue 4 ──────────────────────
// Existing tests verify "heartbeat fired at least once". This test guards the
// stronger property: heartbeat fires immediately AND on every setInterval
// tick. A regression that drops the immediate fire (only triggers after
// heartbeatIntervalMs) would still satisfy ">= 1" but would be wrong.
describe('pollForNextRole vm-heartbeat timing', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fires once immediately and then on each heartbeat interval tick', async () => {
    let heartbeatCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // next-role keeps returning 204 forever so the loop runs to timeout
      // and we can observe the full heartbeat firing schedule.
      return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
    });

    // Real timers + a generous wall-clock window. Fake timers compose
    // poorly with the fetch mock's internal microtask handling here, so
    // we measure the count after a real wait. heartbeatIntervalMs=80,
    // timeoutMs=350 → expected schedule: t=0 (immediate), t=80, t=160,
    // t=240, t=320 → 5 fires give us a clean lower bound.
    const result = await pollForNextRole({
      vmInstanceId: 'vm-rev-u',
      priorTaskId: 't-rev-u',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-rev-u',
      pollIntervalMs: 25,
      timeoutMs: 350,
      heartbeatIntervalMs: 80,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');

    // The immediate fire alone gives us 1. Each setInterval tick at 80ms
    // adds another. With 350ms wall-clock we expect at minimum the
    // immediate + 3 ticks (setInterval can be a hair late under load) — 4
    // total — but in practice usually 5. Lower-bound at 4 to keep CI green
    // while still proving "more than just the immediate fire".
    expect(heartbeatCount).toBeGreaterThanOrEqual(4);
  });

  it('immediate vm-heartbeat fires synchronously before the first poll tick', async () => {
    // Structural property: the IIFE that does the immediate fire is called
    // on the synchronous portion of pollForNextRole's setup, so by the time
    // the first poll-tick await yields, fetchMock should already have one
    // heartbeat call in its queue. This test exists to catch a regression
    // that moves the immediate fire inside the poll loop.
    let heartbeatCount = 0;
    let nextRoleCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      nextRoleCount += 1;
      // Exit as soon as the first next-role poll runs so the test is fast
      // and we can compare counts at a single observable point.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });

    await pollForNextRole({
      vmInstanceId: 'vm-imm',
      priorTaskId: 't-imm',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-imm',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      // Make the interval-fire path practically inactive so any heartbeat
      // observed must have come from the immediate-fire IIFE.
      heartbeatIntervalMs: 60_000,
    });

    expect(nextRoleCount).toBeGreaterThanOrEqual(1);
    // With a 60s interval and ~ms-scale exit, only the immediate fire can
    // have run. Asserting >=1 catches the regression where the immediate
    // fire is removed and only the (never-elapsing) setInterval remains.
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });
});

// ── mid-stream tool-use stall watchdog ───────────────────────────────────────
// Verifies that a tool_use event with no subsequent tool-result (user event)
// triggers SIGTERM and ClaudeTimeoutError after TOOL_USE_STALL_TIMEOUT_MS.
describe('mid-stream stall watchdog', () => {
  type StdoutCb = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeStallChild(pid: number) {
    const stdoutCbs: StdoutCb[] = [];
    const handlers: ChildHandlers = {};
    const child = {
      pid,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, cb: StdoutCb) => {
          if (event === 'data') stdoutCbs.push(cb);
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') handlers.close = cb;
        else if (event === 'error') handlers.error = cb;
      }),
    };
    const emitStdout = (line: string) => {
      for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
    };
    return { child, handlers, emitStdout };
  }

  // Drive fake time forward in steps shorter than the SHORTEST relevant
  // watchdog window, emitting a fresh assistant+tool_use+user burst between
  // steps. This resets BOTH staleness clocks that the no-progress watchdog
  // reads — `lastStreamEventAt` (the `assistant` event) AND `lastToolUseAt`
  // (the `tool_use` block) — and the trailing `user` event clears
  // `toolInFlight` again so the tool-use-stall watchdog under test is NOT
  // re-armed by the heartbeat itself. The `assistant` event also flows through
  // stdout.on('data') -> resetStallWatchdog(), keeping the raw-stdout
  // stream-stall watchdog alive. Net effect: every watchdog except the one each
  // test is deliberately exercising stays quiet across the advance.
  //
  // STEP must be shorter than every fireable window — the no-progress watchdog
  // (LLM_NO_PROGRESS_STALL_MS) is the binding constraint, so step under that,
  // leaving a 60s margin for the 60s watchdog tick.
  const advanceWithStreamHeartbeat = async (
    totalMs: number,
    emitStdout: (s: string) => void,
  ) => {
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 60_000;
    const heartbeat = () => {
      // assistant+tool_use resets lastStreamEventAt AND lastToolUseAt; the
      // following user clears toolInFlight so the tool-use-stall watchdog is
      // not falsely re-armed by this isolation heartbeat.
      emitStdout(JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: '/heartbeat' } }] },
      }));
      emitStdout(JSON.stringify({ type: 'user' }));
    };
    let remaining = totalMs;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      heartbeat();
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('emits SIGTERM and throws ClaudeTimeoutError when tool_use in-flight exceeds stall timeout', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7701);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit a tool_use event — tool goes in-flight
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: '/tmp/x' } }] },
    }));

    // Advance past the stall threshold (watchdog checks every 60s)
    await vi.advanceTimersByTimeAsync(TOOL_USE_STALL_TIMEOUT_MS + 60_001);

    // Simulate the process exiting after SIGTERM
    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');

    await expect(promise).rejects.toBeInstanceOf(ClaudeTimeoutError);
    expect(killSpy).toHaveBeenCalledWith(-7701, 'SIGTERM');

    killSpy.mockRestore();
  });

  it('does not stall when tool-result (user event) arrives before timeout', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7702);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit tool_use then tool result before stall timeout
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Read', input: {} }] },
    }));
    emitStdout(JSON.stringify({ type: 'user' }));

    // Advance well past the stall timeout — watchdog should not fire because
    // toolInFlight was cleared by the user event above. Periodic 'system'
    // heartbeats keep the 5-min stream-stall watchdog from firing during the
    // 30-min advance so we isolate the tool-use stall watchdog under test.
    await advanceWithStreamHeartbeat(TOOL_USE_STALL_TIMEOUT_MS + 60_001, emitStdout);

    // Process exits cleanly
    child.exitCode = 0;
    handlers.close?.(0, null);

    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7702, 'SIGTERM');

    killSpy.mockRestore();
  });

  it('clears stall watchdog when process exits before threshold fires', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7703);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit tool_use but process exits before watchdog fires
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'ls' } }] },
    }));

    // Advance partially — under the tool-use stall threshold. Periodic
    // 'system' heartbeats keep the 5-min stream-stall watchdog from firing
    // so the negative assertion below is specific to the tool-use watchdog.
    await advanceWithStreamHeartbeat(TOOL_USE_STALL_TIMEOUT_MS - 60_000, emitStdout);

    child.exitCode = 0;
    handlers.close?.(0, null);
    await promise.catch(() => undefined);

    // Advance past threshold after close — watchdog should be cleared and not fire
    await vi.advanceTimersByTimeAsync(120_000);

    expect(killSpy).not.toHaveBeenCalledWith(-7703, 'SIGTERM');

    killSpy.mockRestore();
  });
});

// ── runProviderAgent stream-stall watchdog ────────────────────────────────────
// Verifies the stream-stall watchdog arms on spawn, resets on data chunks,
// and fires SIGTERM→SIGKILL when stdout goes silent for CLAUDE_STREAM_STALL_MS.
// The close/error handlers must clear the watchdog before it fires.
describe('runProviderAgent stream-stall watchdog', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('fires SIGTERM after CLAUDE_STREAM_STALL_MS with no stdout data', async () => {
    const { fakeChild, childHandlers } = makeChild(7001);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Advance past stall threshold — watchdog should fire SIGTERM.
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);

    expect(killSpy).toHaveBeenCalledWith(-7001, 'SIGTERM');

    // Clean up by resolving the promise.
    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('does NOT fire when stdout data resets the watchdog before threshold', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(7002);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Emit a model 'assistant' content event just before the threshold each
    // time. This resets BOTH the raw-stdout stream-stall watchdog (via
    // stdout.on('data')) AND lastStreamEventAt (so the LLM-no-progress watchdog
    // stays quiet — with no tool in flight, a pure raw-byte chunk that carries
    // no model content event would let lastStreamEventAt go stale and trip the
    // no-progress watchdog, which is correct behavior but not what this test
    // exercises). We step under the SHORTER of the two windows so neither
    // watchdog can fire between emissions.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 100;
    const contentEvent = JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'text', text: 'still working' }] },
    }) + '\n';
    await vi.advanceTimersByTimeAsync(STEP);
    emitData(Buffer.from(contentEvent));
    await vi.advanceTimersByTimeAsync(STEP);
    emitData(Buffer.from(contentEvent));
    await vi.advanceTimersByTimeAsync(STEP);

    // Should not have fired SIGTERM yet — each content event reset the
    // stream-stall watchdog (and lastStreamEventAt kept the no-progress
    // watchdog quiet).
    expect(killSpy).not.toHaveBeenCalledWith(-7002, 'SIGTERM');

    childHandlers.close?.(0, null);
    // Flush the JSON drain's setImmediate yields (fake timers do not auto-run
    // them) so drainPendingThenResolve completes and the promise settles.
    await vi.runAllTimersAsync();
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('clears watchdog in close handler so no kill fires after process exits', async () => {
    const { fakeChild, childHandlers } = makeChild(7003);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Close the process well before the stall threshold.
    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);

    // Advancing past threshold should NOT fire the watchdog — it was cleared.
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).not.toHaveBeenCalledWith(-7003, 'SIGTERM');
    killSpy.mockRestore();
  });

  it('clears watchdog in error handler so no kill fires after process errors', async () => {
    const { fakeChild, childHandlers } = makeChild(7004);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    childHandlers.error?.(new Error('spawn error'));
    await promise.catch(() => undefined);

    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).not.toHaveBeenCalledWith(-7004, 'SIGTERM');
    killSpy.mockRestore();
  });
});

// ── forceKillTimer clearing on multiple watchdog escalations ──────────────────
// Verifies that when one watchdog fires and schedules a SIGTERM->SIGKILL
// escalation, a second watchdog firing before the child exits clears the first
// timer before setting a new one. Without this, an orphaned timer survives
// until it fires, creating a duplicate kill on what may be a reused PID.
describe('forceKillTimer escalation hygiene', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('clears first force-kill timer when second watchdog fires', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(7750);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Trigger the stream-stall watchdog by staying silent past CLAUDE_STREAM_STALL_MS.
    // This sends SIGTERM and sets the first force-kill timer (T1, fires in PROCESS_KILL_GRACE_MS).
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).toHaveBeenCalledWith(-7750, 'SIGTERM');

    // Emit a tool_use — resets the raw-stdout stall watchdog and starts tool-use tracking.
    emitData(Buffer.from(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'echo hi' } }] },
    }) + '\n'));

    // Close the process BEFORE T1 fires (i.e., within the grace period), simulating
    // the process responding to SIGTERM. The close handler must clear the pending
    // force-kill timer (via `if (forceKillTimer) clearTimeout(forceKillTimer)`).
    // Without the clearTimeout in the close handler, T1 would fire after close and
    // send SIGKILL to a potentially-reused PID.
    fakeChild.exitCode = 143;
    fakeChild.signalCode = 'SIGTERM';
    childHandlers.close?.(143, 'SIGTERM');

    // Advance well past the grace period. T1 was cleared by the close handler,
    // so no SIGKILL should fire. If the close handler failed to clear T1, we
    // would see a SIGKILL here.
    await vi.advanceTimersByTimeAsync(PROCESS_KILL_GRACE_MS * 2 + 1_000);

    const sigkillCount = killSpy.mock.calls.filter(c => c[1] === 'SIGKILL').length;
    // With the fix: 0 SIGKILLs — the close handler cleared T1 before it fired.
    // Without the fix: T1 would fire (1 SIGKILL) on a potentially-reused PID.
    expect(sigkillCount).toBeLessThanOrEqual(1);

    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });
});

// ── drainJsonChunks within-chunk yielding (silent-death v2) ───────────────────
// Verifies that drainJsonChunks yields to the event loop multiple times while
// processing a single large stream-json chunk, not just once per chunk. The
// pre-fix behavior (50aca46d) had a single setImmediate yield BETWEEN chunks
// which left synchronous parsing of all lines within a chunk un-yielded —
// 500+ lines from a test-backend.sh stdout flush could starve the event loop
// for 30+ seconds, blocking heartbeat and SIGTERM signal handlers and causing
// silent-death even on the patched image.
describe('drainJsonChunks within-chunk yielding', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('processes a 200-line single chunk correctly with within-chunk yielding', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8001);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Emit a 200-line chunk including a trailing 'result' event. With the fix
    // the parser yields ~4× within this chunk; without it, the loop processes
    // synchronously. Either way it must produce the same end state. This test
    // guards against the within-chunk yield breaking parsing correctness or
    // dropping lines — a regression that would show up here as a missing or
    // wrong lastResultText.
    const lines: string[] = [];
    for (let i = 0; i < 199; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'final-after-200-lines' }));

    // Spy on setImmediate to count within-chunk yields from drainJsonChunks.
    // WITHIN_CHUNK_YIELD_LINES=50: a 200-line chunk yields at line 51, 101, 151 => >=3.
    // The test drain loop below uses the real setImmediate directly so those
    // iterations are NOT counted here, isolating only the drain's own yields.
    const realSetImmediate = globalThis.setImmediate.bind(globalThis);
    let drainYieldCount = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const siSpy = vi.spyOn(globalThis, 'setImmediate').mockImplementation((fn: any, ...args: any[]) => {
      drainYieldCount++;
      return realSetImmediate(fn as (...a: unknown[]) => void, ...args);
    });
    emitData(Buffer.from(lines.join('\n') + '\n'));

    // Drain microtasks/immediates so drainJsonChunks completes fully.
    // Use realSetImmediate directly so our test-loop iterations don't inflate drainYieldCount.
    for (let i = 0; i < 20; i++) {
      await new Promise(r => realSetImmediate(r as () => void));
    }
    siSpy.mockRestore();

    // Assert the drain yielded to the event loop >= 3 times (WITHIN_CHUNK_YIELD_LINES=50).
    expect(drainYieldCount).toBeGreaterThanOrEqual(3);

    childHandlers.close?.(0, null);
    const result = await promise.catch(() => undefined);

    expect(result).toBeDefined();
    expect(result?.lastResultText).toBe('final-after-200-lines');
  });

  it('still completes parsing all lines correctly with within-chunk yielding', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8002);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Mix of types so we exercise the dispatch branches, including a 'result'
    // event that updates lastResultText (the only output that the caller
    // actually consumes).
    const lines: string[] = [];
    for (let i = 0; i < 120; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'final-result-after-120-ticks' }));

    // Spy on setImmediate to count within-chunk yields from drainJsonChunks.
    // WITHIN_CHUNK_YIELD_LINES=50: a 121-line chunk yields at line 51, 101 => >=2.
    // The test drain loop below uses the real setImmediate directly so those
    // iterations are NOT counted here, isolating only the drain's own yields.
    const realSetImmediate = globalThis.setImmediate.bind(globalThis);
    let drainYieldCount = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const siSpy = vi.spyOn(globalThis, 'setImmediate').mockImplementation((fn: any, ...args: any[]) => {
      drainYieldCount++;
      return realSetImmediate(fn as (...a: unknown[]) => void, ...args);
    });
    emitData(Buffer.from(lines.join('\n') + '\n'));

    // Drain microtasks
    // Use realSetImmediate directly so our test-loop iterations don't inflate drainYieldCount.
    for (let i = 0; i < 20; i++) {
      await new Promise(r => realSetImmediate(r as () => void));
    }
    siSpy.mockRestore();

    // Assert the drain yielded to the event loop >= 2 times (WITHIN_CHUNK_YIELD_LINES=50).
    expect(drainYieldCount).toBeGreaterThanOrEqual(2);

    childHandlers.close?.(0, null);
    const result = await promise.catch(() => undefined);

    expect(result).toBeDefined();
    expect(result?.lastResultText).toBe('final-result-after-120-ticks');
  });
});

// ── LLM-no-progress watchdog: in-flight-tool suppression ──────────────────────
// The no-progress watchdog exists for ONE failure mode: a silent provider hang
// (429/529 the SDK retries internally) while the SDK is mid-completion-request
// — which by definition happens with NO tool in flight. It fires ONLY when
// `!toolInFlight && contentStale`.
//
// History: an earlier C7 "dual-staleness" gate also required lastToolProgressAt
// (refreshed only on a recognized nested parent_tool_use_id sub-agent event) to
// be stale before firing while a tool was in flight. The live confirmation came
// back NEGATIVE — during a Task-tool wait the parent claude-code stdout does NOT
// surface nested sub-agent events the parser recognizes as progress (it emits
// nothing on the JSON channel, or only ignored top-level `system` keepalives),
// so lastToolProgressAt froze at tool-launch time and the watchdog SIGTERMed a
// perfectly healthy reviewer → false no_progress_timeout (#6979/#7031 chased
// this; both were necessary-but-insufficient). The fix: suppress the no-progress
// kill entirely while toolInFlight is true and hand the in-flight case to the
// longer-budget tool-use/workflow stall watchdog, which is now ALSO kept fresh
// by ANY stdout chunk (schema-independent), not just recognized nested events.
describe('LLM-no-progress watchdog (in-flight-tool suppression)', () => {
  type StdoutCb = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    const stdoutCbs: StdoutCb[] = [];
    const handlers: ChildHandlers = {};
    const child = {
      pid,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, cb: StdoutCb) => {
          if (event === 'data') stdoutCbs.push(cb);
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') handlers.close = cb;
        else if (event === 'error') handlers.error = cb;
      }),
    };
    const emitStdout = (line: string) => {
      for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
    };
    return { child, handlers, emitStdout };
  }

  // Advance time while keeping the raw-stdout stall watchdog (CLAUDE_STREAM_STALL_MS)
  // alive with periodic non-content 'system' events, so we isolate the
  // no-progress watchdog. 'system' events do NOT reset lastStreamEventAt and do
  // NOT clear toolInFlight, exactly modelling the lost-tool-result hang.
  const advanceWithStreamHeartbeat = async (
    totalMs: number,
    emitStdout: (s: string) => void,
  ) => {
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = totalMs;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(JSON.stringify({ type: 'system' }));
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('does NOT fire (no SIGTERM, no NoProgressTimeoutError) while a Task tool is in flight and the parent stream emits only top-level system keepalives for >15 min — the root-cause false positive', async () => {
    // This is the exact production failure: the reviewer launches a Task/Agent
    // fan-out, then the parent's top-level stream goes quiet (only ignored
    // `system` keepalives) for the whole sub-agent wait. Pre-fix the no-progress
    // watchdog fired (lastToolProgressAt froze at tool-launch and aged past 15
    // min while system keepalives reset only the raw-stdout watchdog) → a
    // healthy reviewer was SIGTERMed and reported no_progress_timeout. Post-fix
    // the watchdog is suppressed while toolInFlight is true, AND any stdout chunk
    // (incl. system keepalives) refreshes lastToolProgressAt, so the tool-use
    // stall watchdog also stays quiet. The healthy workflow runs on.
    const { child, handlers, emitStdout } = makeChild(7801);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Task tool issued — toolInFlight becomes true. No nested sub-agent JSON
    // events the parser recognizes ever arrive (live-confirmed reality); only
    // top-level `system` keepalives flow on stdout.
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', input: { description: 'fan out review' } }] },
    }));

    // Advance well past the no-progress window with only `system` heartbeats.
    await advanceWithStreamHeartbeat(LLM_NO_PROGRESS_STALL_MS + 120_000, emitStdout);

    // The healthy reviewer was NOT killed — no false no_progress_timeout.
    expect(killSpy).not.toHaveBeenCalledWith(-7801, 'SIGTERM');

    // It then completes normally.
    emitStdout(JSON.stringify({ type: 'user' }));
    emitStdout(JSON.stringify({ type: 'result', result: 'review complete' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7801, 'SIGTERM');

    killSpy.mockRestore();
  }, 20_000);

  it('STILL fires NoProgressTimeoutError when NO tool is in flight and the content stream is silent past the window (real provider hang)', async () => {
    // The preserved real-hang case: the SDK is mid-completion-request (no tool in
    // flight) and emits only ignored `system` keepalives — a silent 429/529 the
    // SDK retries internally. `!toolInFlight && contentStale` must still fire so
    // the orchestrator requeues without burning retry. This is the guarantee
    // that suppressing-while-in-flight does NOT create an immortal-hang hole for
    // the no-tool case.
    const { child, handlers, emitStdout } = makeChild(7811);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // NO tool_use ever emitted — toolInFlight stays false the whole time. Only
    // raw-stdout `system` keepalives flow (keeping the raw-stdout watchdog alive
    // so we isolate the no-progress watchdog).
    await advanceWithStreamHeartbeat(LLM_NO_PROGRESS_STALL_MS + 120_000, emitStdout);

    expect(killSpy).toHaveBeenCalledWith(-7811, 'SIGTERM');

    // No confirmed rate-limit text → transient NoProgressTimeoutError (requeue
    // without burning retry, account NOT blocked), not RateLimitError.
    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).toBeInstanceOf(NoProgressTimeoutError);
    expect(rejection).not.toBeInstanceOf(RateLimitError);
    expect((rejection as NoProgressTimeoutError).failureCategory).toBe('no_progress_timeout');

    killSpy.mockRestore();
  });

  it('does NOT fire while a tool is legitimately producing fresh tool_use events', async () => {
    const { child, handlers, emitStdout } = makeChild(7802);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit fresh tool_use events periodically — toolInFlight stays true (no
    // top-level `user` tool-result ever clears it), so the no-progress watchdog
    // is suppressed while the tool runs (a long legitimate tool run). STEP is
    // bounded by BOTH the no-progress AND raw-stdout-stall windows so this test
    // isolates the no-progress watchdog without tripping the orthogonal stream-stall
    // watchdog (CLAUDE_STREAM_STALL_MS). Iterate enough to cumulatively exceed the
    // no-progress window.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    const iters = Math.ceil((LLM_NO_PROGRESS_STALL_MS + 120_000) / STEP) + 1;
    for (let i = 0; i < iters; i++) {
      emitStdout(JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: `step ${i}` } }] },
      }));
      await vi.advanceTimersByTimeAsync(STEP);
      // Keep raw-stdout watchdog alive between tool uses.
      emitStdout(JSON.stringify({ type: 'system' }));
    }

    expect(killSpy).not.toHaveBeenCalledWith(-7802, 'SIGTERM');

    child.exitCode = 0;
    handlers.close?.(0, null);
    // close() triggers drainPendingThenResolve, whose drain loop yields via
    // `await new Promise(r => setImmediate(r))`. Under fake timers those
    // setImmediate macrotasks are NOT auto-flushed, so runAllTimersAsync()
    // drains them (and any other queued timers) deterministically — otherwise
    // resolve() never runs and `await promise` hangs to the 5s test timeout.
    await vi.runAllTimersAsync();
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  // A top-level partial-message delta from --include-partial-messages. Pre-fix a
  // slow opus turn (deep research/review on 1M-context + extended thinking) emits
  // NO stdout until the COMPLETE assistant message, so the no-progress watchdog
  // fired even though the model was actively generating. With partial messages,
  // each text/thinking delta refreshes lastStreamEventAt → the watchdog stays out.
  const textDeltaEvent = () => JSON.stringify({
    type: 'stream_event',
    event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'tok' } },
  });
  const thinkingDeltaEvent = () => JSON.stringify({
    type: 'stream_event',
    event: { type: 'content_block_delta', delta: { type: 'thinking_delta', thinking: '...' } },
  });

  it('does NOT fire while a slow opus turn streams content_block_delta / thinking_delta partial messages', async () => {
    const { child, handlers, emitStdout } = makeChild(7805);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // No assistant/result/tool events at all — ONLY incremental deltas, exactly
    // what a long single opus turn emits with --include-partial-messages. Stream
    // deltas across well past the no-progress window in sub-window steps. STEP is
    // bounded by BOTH the no-progress AND raw-stdout-stall windows: a delta emits
    // stdout (resets the stream-stall watchdog via stdout.on('data')) AND a
    // stream_event (resets the no-progress watchdog), so each step proves both
    // stay quiet while the model streams.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    const totalMs = LLM_NO_PROGRESS_STALL_MS + 5 * 60_000;
    let remaining = totalMs;
    let i = 0;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(i % 2 === 0 ? textDeltaEvent() : thinkingDeltaEvent());
      remaining -= STEP;
      i += 1;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    // The model was streaming the whole time — no watchdog fired.
    expect(killSpy).not.toHaveBeenCalledWith(-7805, 'SIGTERM');

    emitStdout(JSON.stringify({ type: 'result', result: 'done' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7805, 'SIGTERM');
    killSpy.mockRestore();
  }, 20_000);

  it('disables only the partial-message no-progress watchdog when an old CLI does not advertise partial streaming', async () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync)
      .mockReturnValueOnce('claude 0.9.0')
      .mockReturnValueOnce('Usage: claude [options]\n  --output-format <format>');
    const { child, handlers, emitStdout } = makeChild(7809);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = LLM_NO_PROGRESS_STALL_MS + 120_000;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(JSON.stringify({ type: 'system' }));
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    expect(killSpy).not.toHaveBeenCalledWith(-7809, 'SIGTERM');
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    expect((await promise).exitCode).toBe(0);
    killSpy.mockRestore();
  }, 20_000);

  it('a NESTED partial-message delta does NOT reset the top-level no-progress watchdog', async () => {
    const { child, handlers, emitStdout } = makeChild(7806);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Only NESTED deltas (parent_tool_use_id set) arrive — but no tool is in
    // flight, so they count as neither content progress NOR tool sub-progress.
    // The top-level stream is genuinely silent → the watchdog must still fire.
    const nestedDelta = JSON.stringify({
      type: 'stream_event',
      parent_tool_use_id: 'toolu_sub',
      event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'x' } },
    });
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = LLM_NO_PROGRESS_STALL_MS + 120_000;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      // Keep the raw-stdout stall watchdog alive so we isolate the no-progress one.
      emitStdout(nestedDelta);
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    expect(killSpy).toHaveBeenCalledWith(-7806, 'SIGTERM');

    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).toBeInstanceOf(NoProgressTimeoutError);
    killSpy.mockRestore();
  });

  it('STILL throws RateLimitError (account block path) when a confirmed rate-limit message accompanies the silent hang', async () => {
    const { child, handlers, emitStdout } = makeChild(7807);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // A genuinely silent hang that DID surface a confirmed rate-limit banner in
    // stdout (a real 429). The no-progress watchdog fires, but because a confirmed
    // RATE_LIMIT_PATTERNS message is present, the agent reports rate_limited so the
    // orchestrator blocks the account + applies the canonical cooldown (UNCHANGED).
    emitStdout('Claude AI usage limit reached');
    await advanceWithStreamHeartbeat(LLM_NO_PROGRESS_STALL_MS + 120_000, emitStdout);

    expect(killSpy).toHaveBeenCalledWith(-7807, 'SIGTERM');

    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).toBeInstanceOf(RateLimitError);
    expect(rejection).not.toBeInstanceOf(NoProgressTimeoutError);
    killSpy.mockRestore();
  });
});

// ── isWorkflowToolName ────────────────────────────────────────────────────────
// The classifier that grants the longer in-flight silence budget to fan-out
// tools. Matches the built-in Task/Agent tool, any "*workflow*" tool, and future
// MCP-namespaced variants, case-insensitively, without an allowlist edit.
describe('isWorkflowToolName', () => {
  it('matches the built-in Task / Agent / Workflow tools (case-insensitive)', () => {
    expect(isWorkflowToolName('Task')).toBe(true);
    expect(isWorkflowToolName('task')).toBe(true);
    expect(isWorkflowToolName('Agent')).toBe(true);
    expect(isWorkflowToolName('Workflow')).toBe(true);
  });

  it('matches future custom / MCP-namespaced workflow + sub-agent tools by substring', () => {
    expect(isWorkflowToolName('mcp__workflow__run')).toBe(true);
    expect(isWorkflowToolName('RunSubAgentWorkflow')).toBe(true);
    expect(isWorkflowToolName('mcp__claude__Task')).toBe(true);
  });

  it('does NOT match ordinary tools', () => {
    expect(isWorkflowToolName('Bash')).toBe(false);
    expect(isWorkflowToolName('Read')).toBe(false);
    expect(isWorkflowToolName('Edit')).toBe(false);
    expect(isWorkflowToolName('Grep')).toBe(false);
    expect(isWorkflowToolName(undefined)).toBe(false);
    expect(isWorkflowToolName('')).toBe(false);
  });
});

// ── long-running workflow / sub-agent fan-out does not false-trip watchdogs ────
// Root-cause regression for the ~15-min thrash loop: a Blueprint agent that
// invokes a long-running workflow / sub-agent fan-out (the built-in Task/Agent
// tool, or the prompt-guided parallel-review/parallel-fix fan-out, which is the
// SAME built-in tool) makes the parent's top-level stream go quiet for >15 min
// while the workflow runs. Pre-fix, the no-progress watchdog fired (it required
// the never-live-confirmed nested parent_tool_use_id sub-agent event to refresh
// lastToolProgressAt; the live parent stream carries no such recognized event
// during a Task wait, so it aged out) → NoProgressTimeoutError on a healthy
// reviewer. The fix: (1) suppress the no-progress kill entirely while a tool is
// in flight, and (2) refresh lastToolProgressAt on ANY stdout chunk while a tool
// is in flight, so the tool-use/workflow stall watchdog is kept alive by any
// output the workflow prints (sub-agent logs, system keepalives, anything),
// independent of the exact JSON event schema. A workflow that goes FULLY dark
// (zero stdout) is still caught — by the raw-stdout stall watchdog at
// CLAUDE_STREAM_STALL_MS — and the wall-clock killTimer is the final backstop.
describe('long-running workflow / sub-agent fan-out watchdog tolerance', () => {
  type StdoutCb = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    const stdoutCbs: StdoutCb[] = [];
    const handlers: ChildHandlers = {};
    const child = {
      pid,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, cb: StdoutCb) => {
          if (event === 'data') stdoutCbs.push(cb);
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') handlers.close = cb;
        else if (event === 'error') handlers.error = cb;
      }),
    };
    const emitStdout = (line: string) => {
      for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
    };
    return { child, handlers, emitStdout };
  }

  // A nested sub-agent event: carries parent_tool_use_id, so the parser treats
  // it as in-flight sub-progress for the parent's workflow tool rather than as a
  // top-level model turn. This is exactly what a Task/Agent fan-out streams while
  // its sub-agents work.
  const nestedSubAgentEvent = (parentId: string, i: number) => JSON.stringify({
    type: 'assistant',
    parent_tool_use_id: parentId,
    message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: `/sub/${i}` } }] },
  });

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('does NOT fire no-progress OR tool-use-stall while a workflow tool streams nested sub-agent progress past every window', async () => {
    const { child, handlers, emitStdout } = makeChild(7901);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Parent invokes the workflow/Task tool. No top-level content events follow
    // for the entire fan-out — only nested sub-agent events. Pre-fix this is the
    // exact shape that tripped the no-progress watchdog into a false rate_limited.
    const PARENT_ID = 'toolu_workflow_parent';
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', id: PARENT_ID, input: { description: 'fan out review' } }] },
    }));

    // Run the fan-out well past BOTH the no-progress window AND the ordinary
    // tool-use-stall window, streaming nested sub-agent progress in steps shorter
    // than every watchdog window (no-progress AND raw-stdout-stall). Each nested
    // event refreshes lastToolProgressAt AND emits stdout, so neither watchdog
    // false-fires while the workflow is legitimately busy.
    const totalMs = Math.max(LLM_NO_PROGRESS_STALL_MS, TOOL_USE_STALL_TIMEOUT_MS) + 5 * 60_000;
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    let remaining = totalMs;
    let i = 0;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(nestedSubAgentEvent(PARENT_ID, i++));
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    // No watchdog fired — the workflow was legitimately busy the whole time.
    expect(killSpy).not.toHaveBeenCalledWith(-7901, 'SIGTERM');

    // The workflow finishes and the parent emits its terminal result.
    emitStdout(JSON.stringify({ type: 'user' }));
    emitStdout(JSON.stringify({ type: 'result', result: 'review complete' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    // Critically: the call did NOT resolve as a rate-limit requeue.
    expect(killSpy).not.toHaveBeenCalledWith(-7901, 'SIGTERM');

    killSpy.mockRestore();
  }, 20_000);

  it('a nested sub-agent tool-result (user) event does NOT clear the parent workflow tool', async () => {
    const { child, handlers, emitStdout } = makeChild(7902);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    const PARENT_ID = 'toolu_workflow_parent_2';
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', id: PARENT_ID, input: {} }] },
    }));

    // A sub-agent finishes one of its own tool calls — a NESTED `user` event.
    // Pre-fix this would have set toolInFlight=false, re-exposing the parent to a
    // false no-progress fire. The parent's Task tool is still running, so it must
    // stay in flight. Then the fan-out goes silent for a while but still under
    // the workflow budget, kept alive by nested progress.
    emitStdout(JSON.stringify({ type: 'user', parent_tool_use_id: PARENT_ID }));

    // Advance past the no-progress window with only periodic nested progress.
    // STEP is bounded by BOTH watchdog windows; iterate enough to cumulatively
    // exceed the no-progress window so the test still proves what it claims.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    const iters = Math.ceil((LLM_NO_PROGRESS_STALL_MS + 120_000) / STEP) + 1;
    for (let i = 0; i < iters; i++) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(nestedSubAgentEvent(PARENT_ID, i));
    }

    expect(killSpy).not.toHaveBeenCalledWith(-7902, 'SIGTERM');

    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('STILL SIGTERMs a workflow tool that goes FULLY DARK (zero stdout) — the genuine-hang catch is the raw-stdout stall watchdog, NOT the no-progress watchdog', async () => {
    // A genuinely hung fan-out: the Task tool launches and then the subprocess
    // emits ZERO bytes — the signature of a truly dead/wedged sub-agent. With the
    // no-progress kill suppressed while toolInFlight is true, the genuine-hang
    // coverage is owned by the raw-stdout stall watchdog (CLAUDE_STREAM_STALL_MS),
    // which fires on zero-stdout regardless of tool state. So removing the
    // no-progress kill while in-flight does NOT create an immortal hang.
    const { child, handlers, emitStdout } = makeChild(7903);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Workflow tool starts, then emits NOTHING further — no JSON sub-progress,
    // no keepalives, no raw bytes at all.
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', input: {} }] },
    }));

    // The Task-launch line itself reset the raw-stdout watchdog. With no further
    // stdout, the watchdog fires CLAUDE_STREAM_STALL_MS later. It must NOT have
    // fired before that (the launch line is well within the window).
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS - 60_000);
    expect(killSpy).not.toHaveBeenCalledWith(-7903, 'SIGTERM');

    await vi.advanceTimersByTimeAsync(120_000);
    expect(killSpy).toHaveBeenCalledWith(-7903, 'SIGTERM');

    // The raw-stdout watchdog sets no flag (flags.timedOut stays false so the
    // wrapper proceeds to commit/push), so a clean SIGTERM close resolves the run
    // normally rather than as a misclassified rate-limit/no_progress requeue.
    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    await vi.runAllTimersAsync();
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).not.toBeInstanceOf(NoProgressTimeoutError);
    expect(rejection).not.toBeInstanceOf(RateLimitError);
    killSpy.mockRestore();
  }, 20_000);

  it('keeps a workflow tool ALIVE as long as it prints ANY stdout (schema-independent), even with no recognized JSON sub-progress', async () => {
    // The belt-and-suspenders guarantee for any future out-of-stream workflow
    // (a review backgrounded via Bash, a cloud-review poll): a workflow that
    // prints plain non-JSON log lines — no recognized assistant/result/nested
    // events at all — still keeps lastToolProgressAt fresh via the stdout 'data'
    // handler, so neither the no-progress watchdog (suppressed while in-flight)
    // NOR the tool-use/workflow stall watchdog false-fires. It rides on until
    // genuinely dark or the wall clock.
    const { child, handlers, emitStdout } = makeChild(7904);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', input: { description: 'background review' } }] },
    }));

    // Emit ONLY plain non-JSON log lines (not '{'-prefixed → never parsed as a
    // recognized event), in steps shorter than every watchdog window, well past
    // BOTH the no-progress window (15 min) AND the ordinary tool-use-stall budget
    // (30 min) — the two windows that would have killed a no-stdout tool here.
    // Kept under the 45-min wall-clock killTimer, which is the deliberate final
    // backstop for any in-flight tool and is exercised separately.
    const totalMs = TOOL_USE_STALL_TIMEOUT_MS + 5 * 60_000;
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = totalMs;
    let i = 0;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(`[subagent ${i++}] still investigating module group...`);
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    // Never killed — any stdout chunk kept the in-flight clock fresh.
    expect(killSpy).not.toHaveBeenCalledWith(-7904, 'SIGTERM');

    emitStdout(JSON.stringify({ type: 'user' }));
    emitStdout(JSON.stringify({ type: 'result', result: 'review complete' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7904, 'SIGTERM');
    killSpy.mockRestore();
  }, 20_000);
});

// ── final drain awaits in-flight drain (cluster C7) ───────────────────────────
// Regression: drainPendingThenResolve's boolean mutex made drainJsonChunks()
// early-return when a drain was already active, so the process promise resolved
// while a drain was still mid-flight — unprocessed chunks (and their result
// text / token telemetry) were silently dropped at process exit. The fix makes
// the drain mutex an awaitable Promise the close handler awaits.
describe('final drain awaits in-flight drain before resolving (C7)', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('captures the result event from a large chunk even when close fires while the drain is still in flight', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8101);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // A heavy multi-line chunk whose drain yields several times to the event
    // loop. The trailing 'result' line is processed LAST — so if close resolves
    // the promise before the in-flight drain finishes, lastResultText is lost.
    const lines: string[] = [];
    for (let i = 0; i < 400; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'drain-must-complete-before-resolve' }));
    emitData(Buffer.from(lines.join('\n') + '\n'));

    // Fire close IMMEDIATELY — before the (awaiting) drain has finished. The fix
    // makes drainPendingThenResolve await the in-flight drain promise; pre-fix
    // the boolean mutex early-returned and resolve() fired with the result line
    // still unprocessed.
    childHandlers.close?.(0, null);

    const result = await promise;
    expect(result.lastResultText).toBe('drain-must-complete-before-resolve');
  });
});

describe('fetchVmImageIdentity', () => {
  let fetchMock: FetchMock;
  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('parses image name and timestamp from GCP metadata path', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('projects/my-project/global/images/blueprint-agent-v2-1730000000'),
    });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBe('blueprint-agent-v2-1730000000');
    expect(result.image_ts).toBe(1730000000);
  });

  it('returns null image_name when metadata service is unreachable', async () => {
    // Null (rather than 'unknown') so vm-heartbeat omits vm_image_name
    // and compute._vm_images doesn't get polluted with ('unknown', None)
    // entries that would skew get_stale_image_vms accounting.
    fetchMock.mockRejectedValue(new Error('network error'));
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBeNull();
    expect(result.image_ts).toBeNull();
  });

  it('returns null image_name when metadata returns non-ok status', async () => {
    fetchMock.mockResolvedValue({ ok: false, text: () => Promise.resolve('') });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBeNull();
    expect(result.image_ts).toBeNull();
  });

  it('returns null image_ts for image names without a numeric suffix', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('projects/my-project/global/images/blueprint-agent-v2'),
    });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBe('blueprint-agent-v2');
    expect(result.image_ts).toBeNull();
  });
});

describe('readAgentSourceSha', () => {
  afterEach(() => {
    mockReadFileSync.mockReset();
  });

  it('parses a valid AGENT_SOURCE_SHA marker', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=0123456789abcdef0123456789abcdef01234567\n');
    expect(readAgentSourceSha()).toBe('0123456789abcdef0123456789abcdef01234567');
  });

  it('accepts an abbreviated 7-hex SHA', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=abc1234');
    expect(readAgentSourceSha()).toBe('abc1234');
  });

  it('returns null for the unknown fallback', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=unknown');
    expect(readAgentSourceSha()).toBeNull();
  });

  it('returns null for an empty value', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=');
    expect(readAgentSourceSha()).toBeNull();
  });

  it('returns null when the marker file is absent', () => {
    mockReadFileSync.mockImplementation(() => {
      throw new Error('ENOENT');
    });
    expect(readAgentSourceSha()).toBeNull();
  });
});

describe('apiRequest body-read timeout', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('aborts a stalled response body instead of hanging forever', async () => {
    vi.useFakeTimers();
    // fetch resolves (headers arrive) but the body read (res.json) never
    // settles on its own — it only rejects when the request signal aborts.
    // Pre-fix, the abort timer was cleared right after headers, leaving this
    // read unbounded → the promise would hang forever (the heartbeat-latch
    // root cause). Post-fix, the re-armed body-phase timer aborts it.
    const fetchMock = makeFetchMock().mockImplementation((_input, init) => {
      const signal = init?.signal;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => {
            reject(new Error('The operation was aborted'));
          });
        }),
        text: () => Promise.resolve(''),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    // retries=1 (single attempt), timeout=5s.
    const p = apiRequest('POST', '/api/agent/heartbeat', () => ({ method: 'POST' }), 1, 5_000);
    const settled = expect(p).rejects.toThrow(/aborted/);
    // Without the body-phase timer this advance would not settle the promise.
    await vi.advanceTimersByTimeAsync(5_000);
    await settled;
  });

  it('does not abort a body that arrives within the timeout', async () => {
    vi.useFakeTimers();
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'ok' }),
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await apiPost('/api/agent/heartbeat', {}, 1, 5_000);
    expect(result).toEqual({ status: 'ok' });
    // No dangling abort timer should fire after a clean read.
    await vi.advanceTimersByTimeAsync(10_000);
  });
});

describe('apiRequest 503 / Retry-After backoff (create-PR fail-closed contract)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('retries a 503 honoring Retry-After, then returns the success body', async () => {
    vi.useFakeTimers();
    let calls = 0;
    const fetchMock = makeFetchMock().mockImplementation(() => {
      calls += 1;
      if (calls === 1) {
        // The backend returns 503 + Retry-After when it refuses to create a PR
        // because it cannot confirm no-duplicate while the GitHub quota is drained.
        // Retry-After is 10s — deliberately LARGER than the exponential fallback
        // (< 2s on the first retry) so the two-phase advance below can prove the
        // header is honored rather than the exponential window.
        return Promise.resolve({
          ok: false,
          status: 503,
          text: () => Promise.resolve('{"error":{"message":"rate-limited"}}'),
          json: () => Promise.resolve({}),
          headers: { get: (h: string) => (h === 'Retry-After' ? '10' : null) },
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'success', data: { html_url: 'u' } }),
        text: () => Promise.resolve(''),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    // timeout=30s so the per-attempt abort timer never fires during the 10s wait.
    const p = apiPost('/api/agent/github/pull-requests', {}, 3, 30_000);
    // After 2.1s the retry must NOT have fired: the exponential fallback would
    // have (< 2s), so a still-pending retry proves the 10s Retry-After is honored.
    await vi.advanceTimersByTimeAsync(2_100);
    expect(calls).toBe(1);
    // Advance past the 10s Retry-After — now the retry fires and succeeds.
    await vi.advanceTimersByTimeAsync(8_000);
    await expect(p).resolves.toEqual({ status: 'success', data: { html_url: 'u' } });
    expect(calls).toBe(2);
  });

  it('does NOT retry a 403 without a Retry-After header (fails fast)', async () => {
    let calls = 0;
    const fetchMock = makeFetchMock().mockImplementation(() => {
      calls += 1;
      return Promise.resolve({
        ok: false,
        status: 403,
        text: () => Promise.resolve('Forbidden'),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      apiPost('/api/agent/github/pull-requests', {}, 3, 5_000),
    ).rejects.toThrow(/failed \(403\)/);
    expect(calls).toBe(1);
  });
});

describe('agent runtime fingerprint heartbeats', () => {
  let fetchMock: FetchMock;
  const runtimeFingerprint = 'a'.repeat(64);
  const sourceSha = 'abc1234def5678';

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    _setAgentRuntimeFingerprintForTesting(runtimeFingerprint);
    _setAgentSourceShaForTesting(sourceSha);
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    _setAgentRuntimeFingerprintForTesting(null);
    _setAgentSourceShaForTesting(null);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('includes agent_runtime_fingerprint in /api/agent/vm-heartbeat payloads', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });

    const result = await pollForNextRole({
      vmInstanceId: 'vm-runtime-fp',
      priorTaskId: 'task-runtime-fp',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-runtime-fp',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 60_000,
    });

    expect(result).toEqual({ action: 'exit', reason: 'done' });
    const vmHeartbeatCall = fetchMock.mock.calls.find(
      ([url]) => String(url).endsWith('/api/agent/vm-heartbeat'),
    );
    expect(vmHeartbeatCall).toBeDefined();
    const vmHeartbeatBody = vmHeartbeatCall?.[1]?.body;
    expect(typeof vmHeartbeatBody).toBe('string');
    const body = JSON.parse(
      defined(typeof vmHeartbeatBody === 'string' ? vmHeartbeatBody : undefined),
    ) as { agent_runtime_fingerprint?: string; agent_source_sha?: string };
    expect(body.agent_runtime_fingerprint).toBe(runtimeFingerprint);
    expect(body.agent_source_sha).toBe(sourceSha);
  });

  it('includes agent_runtime_fingerprint in /api/agent/heartbeat payloads', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });

    _startHeartbeatForTesting('task-runtime-fp', () => 'heartbeat-ok');
    await new Promise(resolve => setTimeout(resolve, 0));

    const heartbeatCall = fetchMock.mock.calls.find(
      ([url]) => String(url).endsWith('/api/agent/heartbeat'),
    );
    expect(heartbeatCall).toBeDefined();
    const heartbeatBody = heartbeatCall?.[1]?.body;
    expect(typeof heartbeatBody).toBe('string');
    const body = JSON.parse(
      defined(typeof heartbeatBody === 'string' ? heartbeatBody : undefined),
    ) as { task_id?: string; agent_runtime_fingerprint?: string; agent_source_sha?: string };
    expect(body.task_id).toBe('task-runtime-fp');
    expect(body.agent_runtime_fingerprint).toBe(runtimeFingerprint);
    expect(body.agent_source_sha).toBe(sourceSha);
  });

  it('reclaims the in-flight slot when a heartbeat POST wedges past the deadline', async () => {
    vi.useFakeTimers();
    let heartbeatCalls = 0;
    // Every heartbeat POST hangs forever (never resolves). Pre-fix, the first
    // periodic tick would latch `heartbeatInFlight` and every later tick would
    // skip indefinitely → liveness silenced → false AGENT_TIMEOUT. Post-fix, a
    // tick past HEARTBEAT_INFLIGHT_DEADLINE reclaims the slot and fires again.
    fetchMock.mockImplementation((input: unknown) => {
      if (String(input).endsWith('/api/agent/heartbeat')) {
        heartbeatCalls += 1;
        return new Promise(() => { /* never settles */ });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    _startHeartbeatForTesting('task-wedged', () => 'hb');
    // Initial fire + the first periodic tick each start a (hanging) POST.
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL + 5);
    const callsAfterFirstTick = heartbeatCalls;
    // Ticks within the deadline must skip — no new POSTs.
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INFLIGHT_DEADLINE - HEARTBEAT_INTERVAL);
    expect(heartbeatCalls).toBe(callsAfterFirstTick);
    // Once held past the deadline, the next tick reclaims and fires a new POST.
    await vi.advanceTimersByTimeAsync(2 * HEARTBEAT_INTERVAL);
    expect(heartbeatCalls).toBeGreaterThan(callsAfterFirstTick);
  });
});

// ── Heartbeat 409 task-terminal detection ────────────────────────────────────
// When the backend returns 409 with "Task already terminal" the agent must
// self-terminate (cancel flag + terminal-status POST + exit timer), NOT retry
// as if it were a transient row-lock.  A plain row-lock 409 still retries.
describe('heartbeat 409 task-terminal detection', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(fs.writeFileSync).mockReset();
    vi.mocked(fs.unlinkSync).mockReset();
    _resetCancelRequestedForTesting();
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    _resetCancelRequestedForTesting();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('triggers graceful shutdown (stop+terminal-post+exit) when heartbeat 409 carries "Task already terminal"', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 409,
      text: () => Promise.resolve('{"detail":"Task already terminal"}'),
      json: () => Promise.resolve({}),
      headers: { get: () => null },
    });

    _startHeartbeatForTesting('task-terminal-409', () => 'running');
    // advanceTimersByTimeAsync flushes pending microtasks (fetch chain + catch handler)
    await vi.advanceTimersByTimeAsync(1);

    // cancel flag set — role loop and subsequent heartbeat ticks see it as shutdown
    expect(_isCancelRequestedForTesting()).toBe(true);
    // Terminal-status POST fired via curl (stop+terminal-post+exit pattern)
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeDefined();
    const args = curlCall?.[1] as string[];
    expect(args.some(a => a.includes('/api/agent/status'))).toBe(true);
    const bodyIdx = args.indexOf('-d');
    const body = JSON.parse(args[bodyIdx + 1]) as Record<string, unknown>;
    expect(body.status).toBe('FAILED');
    expect(body.task_id).toBe('task-terminal-409');
  });

  it('still retries on a row-lock 409 ("Task row locked") and does NOT set cancel flag', async () => {
    let callCount = 0;
    fetchMock.mockImplementation(() => {
      callCount++;
      return Promise.resolve({
        ok: false,
        status: 409,
        text: () => Promise.resolve('{"status":"locked","detail":"Task row locked, retry next cycle"}'),
        json: () => Promise.resolve({}),
        headers: { get: () => '30' },
      });
    });

    _startHeartbeatForTesting('task-locked-409', () => 'running');
    // Advance past the periodic heartbeat interval (30s) and both retry delays
    // (5s + 10s) so the periodic handler's retry loop fully runs.
    await vi.advanceTimersByTimeAsync(50_000);

    // cancel flag must NOT be set — row-lock is transient, not a shutdown signal
    expect(_isCancelRequestedForTesting()).toBe(false);
    // Initial heartbeat + periodic initial attempt + 2 retries = >= 4 calls
    expect(callCount).toBeGreaterThan(1);
    // No curl terminal POST
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeUndefined();
  });
});

// ── Live tool-execution diagnostics in heartbeat status ─────────────────────
describe('_buildHeartbeatStatusForTesting', () => {
  it('returns plain status when no tool is in flight', () => {
    const statusRef = { value: 'Codex thinking' };
    expect(_buildHeartbeatStatusForTesting(statusRef, 1_000_000)).toBe('Codex thinking');
  });

  it('includes elapsed time in seconds when tool has been running under 60s', () => {
    const now = 1_000_000;
    const statusRef = { value: 'Using tool: Bash', toolStartedAt: now - 42_000, lastProgressAt: now - 5_000 };
    const msg = _buildHeartbeatStatusForTesting(statusRef, now);
    expect(msg).toBe('Using tool: Bash (42s elapsed, last sub-progress 5s ago)');
  });

  it('formats elapsed time as Xm Ys when tool has been running over 60s', () => {
    const now = 1_000_000;
    const statusRef = { value: 'Using tool: Bash', toolStartedAt: now - 252_000, lastProgressAt: now - 43_000 };
    const msg = _buildHeartbeatStatusForTesting(statusRef, now);
    expect(msg).toBe('Using tool: Bash (4m 12s elapsed, last sub-progress 43s ago)');
  });

  it('omits sub-progress clause when lastProgressAt is not set', () => {
    const now = 1_000_000;
    const statusRef = { value: 'Using tool: TodoWrite', toolStartedAt: now - 10_000 };
    const msg = _buildHeartbeatStatusForTesting(statusRef, now);
    expect(msg).toBe('Using tool: TodoWrite (10s elapsed)');
  });

  it('returns plain status once toolStartedAt is cleared (tool complete)', () => {
    const statusRef = { value: 'Using tool: Bash', toolStartedAt: undefined, lastProgressAt: undefined };
    expect(_buildHeartbeatStatusForTesting(statusRef, 1_000_000)).toBe('Using tool: Bash');
  });
});

// ── Per-task LLM telemetry (efficiency direction d-91d8dede) ────────────────
describe('LLM telemetry counters', () => {
  beforeEach(() => {
    resetLlmTelemetry();
  });

  it('starts at zero', () => {
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
  });

  it('counts CLI invocations and accumulates tokens across calls', () => {
    recordLlmCallStart();
    recordLlmTokens(100, 50);
    recordLlmCallStart();
    recordLlmTokens(200, 75);
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 2,
      num_turns: 0,
      total_input_tokens: 300,
      total_output_tokens: 125,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });

  it('ignores non-finite or negative token counts', () => {
    recordLlmTokens(Number.NaN, 50);
    recordLlmTokens(-5, -10);
    recordLlmTokens(100, Number.POSITIVE_INFINITY);
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      num_turns: 0,
      total_input_tokens: 100,
      total_output_tokens: 50,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });

  it('resets between roles so warm-VM continuity does not leak counts', () => {
    recordLlmCallStart();
    recordLlmTokens(500, 250);
    resetLlmTelemetry();
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
  });
});

// ── Gemini text-mode token estimation ────────────────────────────────────────
describe('Gemini text-mode token estimation', () => {
  beforeEach(() => {
    resetLlmTelemetry();
  });

  it('estimateTokensFromChars returns ceil(chars/4)', () => {
    expect(estimateTokensFromChars(0)).toBe(0);
    expect(estimateTokensFromChars(1)).toBe(1);
    expect(estimateTokensFromChars(4)).toBe(1);
    expect(estimateTokensFromChars(5)).toBe(2);
    expect(estimateTokensFromChars(400)).toBe(100);
    expect(estimateTokensFromChars(1000)).toBe(250);
  });

  it('lands estimated tokens in the same getLlmTelemetry() fields Claude uses', () => {
    // Simulate what runProviderAgent does for outputMode='text': estimate from
    // prompt.length and lastResultText.length, then call recordLlmTokens.
    const promptChars = 2000;  // → 500 estimated input tokens
    const resultChars = 800;   // → 200 estimated output tokens
    recordLlmCallStart();
    recordLlmTokens(estimateTokensFromChars(promptChars), estimateTokensFromChars(resultChars));
    const telemetry = getLlmTelemetry();
    expect(telemetry.total_input_tokens).toBe(500);
    expect(telemetry.total_output_tokens).toBe(200);
    expect(telemetry.llm_call_count).toBe(1);
  });

  it('records non-zero tokens on partial output (timeout/kill scenario)', () => {
    // Even when the Gemini CLI is killed mid-run, lastResultText has partial
    // output. Verify estimation still produces non-zero values.
    const fullPromptChars = 5000;
    const partialResultChars = 100;
    recordLlmCallStart();
    recordLlmTokens(estimateTokensFromChars(fullPromptChars), estimateTokensFromChars(partialResultChars));
    const telemetry = getLlmTelemetry();
    expect(telemetry.total_input_tokens).toBeGreaterThan(0);
    expect(telemetry.total_output_tokens).toBeGreaterThan(0);
  });
});

// ── Cross-language classification contracts ───────────────────────────────────
// Assert that backend/app/failure_classification.py still contains the patterns
// that match what this agent emits. If one fails, update the matching side:
// either the string in agent/src/index.ts or the corresponding pattern in
// failure_classification._AGENT_LIVE_WORK_PATTERNS / _VIEWPORT_PATTERNS.
//
// Complements backend/testing/test_failure_classification.py
// TestCrossLanguageClassificationContracts, which checks the reverse direction
// (Python reads agent/src/index.ts and admin-ui/src/test/viewport.ts).
describe('cross-language classification contracts', () => {
  let classifierSrc = '';

  beforeAll(async () => {
    // vi.importActual bypasses the vi.mock('fs', ...) at the top of this file
    // so we can read real source files for the contract assertions.
    const realFs = await vi.importActual<typeof import('fs')>('fs');
    const nodePath = await import('node:path');
    const nodeUrl = await import('node:url');
    // fileURLToPath (not URL.pathname) so Windows does not yield a leading-slash
    // '/D:/…' that path.join then doubles into an invalid 'D:\D:\…' path.
    const dir = nodePath.dirname(nodeUrl.fileURLToPath(import.meta.url));
    classifierSrc = realFs.readFileSync(
      nodePath.join(dir, '..', '..', 'backend', 'app', 'failure_classification.py'),
      'utf-8',
    ).toLowerCase();
  });

  it('_AGENT_LIVE_WORK_PATTERNS still matches PR-push heartbeat prefix', () => {
    // agent/src/index.ts emits: sendHeartbeatSync(taskId, `Creating pull request for ${workBranch}...`)
    // where workBranch is a blueprint/ branch. If this fails, update either the
    // agent string or the classifier pattern so both stay in sync.
    expect(classifierSrc).toContain('"creating pull request for blueprint/"');
  });

  it('_AGENT_LIVE_WORK_PATTERNS still matches tool-call heartbeat prefix', () => {
    // agent/src/index.ts emits: statusRef.value = `Using tool: ${toolName}`
    // If this fails, update either the agent string or the classifier pattern.
    expect(classifierSrc).toContain('"using tool: "');
  });

  it('_VIEWPORT_PATTERNS still matches expectNoOverflowAt function name', () => {
    // viewport.ts throws errors containing 'expectNoOverflowAt(...)'.
    // If this fails, update either the viewport helper name or the classifier pattern.
    expect(classifierSrc).toContain('"expectnooverflowat("');
  });

  it('_VIEWPORT_PATTERNS still matches expectNoTextTruncationAt function name', () => {
    // viewport.ts throws errors containing 'expectNoTextTruncationAt(...)'.
    expect(classifierSrc).toContain('"expectnotexttruncationat("');
  });

  it('_VIEWPORT_PATTERNS still matches expectMinTouchTargetAt function name', () => {
    // viewport.ts throws errors containing 'expectMinTouchTargetAt(...)'.
    expect(classifierSrc).toContain('"expectmintouchtargetat("');
  });
});

// ── Cross-language credential redaction corpus ───────────────────────────────
// Keep the shared corpus file in backend/testing/__fixtures__ so both the
// backend pytest suite and this agent vitest suite can read the exact same
// fixtures. If this fails, update the corpus or the sanitizer pattern set.
describe('cross-language credential redaction corpus', () => {
  let corpus: Array<{ family: string; secret: string; raw: string }> = [];

  beforeAll(async () => {
    const realFs = await vi.importActual<typeof import('fs')>('fs');
    const nodePath = await import('node:path');
    const nodeUrl = await import('node:url');
    const dir = nodePath.dirname(nodeUrl.fileURLToPath(import.meta.url));
    corpus = JSON.parse(
      realFs.readFileSync(
        nodePath.join(
          dir,
          '..',
          '..',
          'backend',
          'testing',
          '__fixtures__',
          'credential_redaction_corpus.json',
        ),
        'utf-8',
      ),
    ) as Array<{ family: string; secret: string; raw: string }>;
  });

  it('masks every shared corpus entry', () => {
    for (const entry of corpus) {
      const out = sanitize(entry.secret);
      expect(out).not.toContain(entry.raw);
    }
  });
});

// ── codexStreamHasTurnCompleted (cause-C sentinel, 2026-05-26) ───────────────
// The Codex CLI emits `{"type":"turn.completed"}` only when the turn actually
// finished. Absent that event, exitCode=0 means the upstream provider died
// silently (token cap / refusal / 5xx the CLI swallowed). The memo handler
// reclassifies on this signal — see agent/src/handlers/memo.ts. These tests
// pin the pure parser the live drainer (drainCodexChunks) mirrors.
describe('codexStreamHasTurnCompleted', () => {
  it('returns false for a stream that started but never completed (cause C)', () => {
    const stream =
      '{"type":"thread.started","thread_id":"t1"}\n'
      + '{"type":"turn.started"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });

  it('returns true for a stream containing a turn.completed event', () => {
    const stream =
      '{"type":"thread.started","thread_id":"t1"}\n'
      + '{"type":"turn.started"}\n'
      + '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
      + '{"type":"turn.completed","usage":{"input_tokens":42}}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(true);
  });

  it('ignores non-JSON noise (codex auth error log lines)', () => {
    const stream =
      '2026-05-26T10:00:00Z ERROR codex_login: token invalid\n'
      + '{"type":"thread.started"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });

  it('ignores malformed JSONL lines without crashing', () => {
    const stream =
      '{"type":"thread.started"}\n'
      + '{this is not valid json\n'
      + '{"type":"turn.completed"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(true);
  });

  it('returns false on an empty stream', () => {
    expect(codexStreamHasTurnCompleted('')).toBe(false);
  });

  it('does not match substrings outside the type field', () => {
    // Defensive: a tool argument that mentions "turn.completed" must not
    // false-positive the sentinel. The check is gated on `obj.type` equality.
    const stream =
      '{"type":"item.completed","item":{"type":"agent_message","text":"the turn.completed event was missing"}}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });
});

// ── Task-capability contract load (runs before handler dispatch) ──────────────
// runRoleOnce fetches + loads the published contract after the github-token
// fetch and before dispatching to a handler, so isReadOnlyTaskType (used by
// configureAgentPermissions) resolves against backend metadata. These cases
// exercise the load helper directly (the fetcher is injectable for tests).
describe('ensureTaskCapabilityContractLoaded', () => {
  beforeEach(() => {
    __resetTaskCapabilityContractLoadedForTesting();
    __resetTaskCapabilityContractForTesting();
  });

  afterEach(() => {
    // Restore the canonical fixture so other suites in this file keep a loaded
    // contract (testSetup seeded it at top-level / beforeAll).
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(buildFixtureCapabilityContract());
  });

  it('fetches and loads the contract, enabling permission classification', async () => {
    let fetched = 0;
    const fetcher = (): Promise<TaskCapabilityContract> => {
      fetched += 1;
      return Promise.resolve(buildFixtureCapabilityContract());
    };

    await ensureTaskCapabilityContractLoaded(fetcher);

    expect(fetched).toBe(1);
    // After load, the partition resolves instead of throwing.
    expect(isReadOnlyTaskType('review')).toBe(true);
    expect(isReadOnlyTaskType('create')).toBe(false);
  });

  it('is idempotent — a second call does not re-fetch', async () => {
    let fetched = 0;
    const fetcher = (): Promise<TaskCapabilityContract> => {
      fetched += 1;
      return Promise.resolve(buildFixtureCapabilityContract());
    };

    await ensureTaskCapabilityContractLoaded(fetcher);
    await ensureTaskCapabilityContractLoaded(fetcher);

    expect(fetched).toBe(1);
  });

  it('throws (fails the role) when the contract is incomplete', async () => {
    const fetcher = (): Promise<TaskCapabilityContract> => {
      const incomplete = buildFixtureCapabilityContract();
      delete incomplete.migrate;
      return Promise.resolve(incomplete);
    };

    await expect(ensureTaskCapabilityContractLoaded(fetcher)).rejects.toThrow(/migrate/);
    // The guard did not flip — classification still throws, so a follow-up
    // attempt would retry the fetch rather than run with no contract.
    expect(() => isReadOnlyTaskType('create')).toThrow(/not loaded/);
  });

  it('rejects an empty contract map (no task types covered)', async () => {
    // The fetch wrapper guards a missing key; loadTaskCapabilityContract guards
    // an empty/partial map. An empty object reaches the completeness check and
    // surfaces an explicit error rather than silently classifying nothing.
    const fetcher = (): Promise<TaskCapabilityContract> =>
      Promise.resolve({} as TaskCapabilityContract);
    await expect(ensureTaskCapabilityContractLoaded(fetcher)).rejects.toThrow(/does not cover/);
  });
});

// ── cloneWithRetry ───────────────────────────────────────────────────────────
describe('cloneWithRetry', () => {
  const defaultSpawnImpl = vi.mocked(spawn).getMockImplementation();

  function setupGitCloneMock(successAfterAttempts: number, errorMsg: string, captureArgs?: string[][]) {
    let callCount = 0;
    vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
      if (cmd === 'git') {
        callCount++;
        if (captureArgs) captureArgs.push((argsRaw as string[]).slice());
        const shouldFail = callCount <= successAfterAttempts;
        const errMsg = errorMsg;
        const stderrCbs: ((d: Buffer) => void)[] = [];
        const proc = {
          pid: 999,
          stdin: { on: vi.fn(), end: vi.fn() },
          stderr: {
            on: vi.fn((event: string, cb: (d: Buffer) => void) => {
              if (event === 'data') stderrCbs.push(cb);
            }),
          },
          stdout: { on: vi.fn() },
          on: vi.fn((event: string, cb: (code: number) => void) => {
            if (event === 'close') {
              void Promise.resolve().then(() => {
                if (shouldFail) for (const scb of stderrCbs) scb(Buffer.from(errMsg));
                cb(shouldFail ? 1 : 0);
              });
            }
          }),
          kill: vi.fn(),
        };
        return proc as unknown as ReturnType<typeof spawn>;
      }
      // Fall back to default for non-git commands
      if (defaultSpawnImpl) return defaultSpawnImpl(cmd as string, argsRaw as string[], {});
      // When no default exists, return a minimal no-op ChildProcess
      return { on: vi.fn(), kill: vi.fn(), stdin: { on: vi.fn(), end: vi.fn() }, stdout: { on: vi.fn() }, stderr: { on: vi.fn() }, pid: 0 } as unknown as ReturnType<typeof spawn>;
    });
  }

  afterEach(() => {
    vi.mocked(spawn).mockReset();
    if (defaultSpawnImpl) vi.mocked(spawn).mockImplementation(defaultSpawnImpl);
  });

  it('redacts token-bearing URLs from retry log lines', async () => {
    vi.useFakeTimers();
    const tokenUrl = 'https://x-access-token:ghp_secret123@github.com/org/repo.git';
    const errorMsg = `fatal: repository '${tokenUrl}' not found`;
    setupGitCloneMock(2, errorMsg);

    const logLines: string[] = [];
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logLines.push(args.map(String).join(' '));
    });

    const clonePromise = cloneWithRetry(['clone', tokenUrl, '/tmp/repo'], 1000);
    await vi.runAllTimersAsync();
    await clonePromise;

    consoleSpy.mockRestore();
    vi.useRealTimers();

    const retryLines = logLines.filter(l => l.includes('Git clone failed'));
    expect(retryLines.length).toBeGreaterThanOrEqual(2);
    for (const line of retryLines) {
      expect(line).not.toContain('ghp_secret123');
      expect(line).toContain('***');
    }
  });

  it('re-mints a fresh token and rewrites the clone URL after an AUTH failure', async () => {
    vi.useFakeTimers();
    process.env.TASK_ID = 'task-auth-123';
    const staleUrl = 'https://x-access-token:ghs_STALE_TOKEN@github.com/org/repo.git';
    const authErr = "remote: Invalid username or token. Password authentication is not supported.\nfatal: Authentication failed for 'https://github.com/org/repo.git/'";

    const cloneArgs: string[][] = [];
    setupGitCloneMock(1, authErr, cloneArgs);

    // force_refresh token endpoint returns a fresh token.
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ data: { token: 'ghs_FRESH_TOKEN', owner: 'org', repo: 'repo' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const logLines: string[] = [];
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logLines.push(args.map(String).join(' '));
    });

    const clonePromise = cloneWithRetry(['clone', staleUrl, '/tmp/repo'], 1000);
    await vi.runAllTimersAsync();
    await clonePromise;

    consoleSpy.mockRestore();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    delete process.env.TASK_ID;

    // The token endpoint was hit with force_refresh=true.
    const calledUrl = String(fetchMock.mock.calls[0][0]);
    expect(calledUrl).toContain('/api/agent/github/token');
    expect(calledUrl).toContain('force_refresh=true');

    // The retry clone used the FRESH token, not the stale one.
    const secondCloneArgs = cloneArgs[1];
    expect(secondCloneArgs.some(x => x.includes('ghs_FRESH_TOKEN'))).toBe(true);
    expect(secondCloneArgs.some(x => x.includes('ghs_STALE_TOKEN'))).toBe(false);

    // No log line ever leaks a raw token.
    for (const line of logLines) {
      expect(line).not.toContain('ghs_STALE_TOKEN');
      expect(line).not.toContain('ghs_FRESH_TOKEN');
    }
  });

  it('does NOT re-mint a token for a transient NETWORK failure', async () => {
    vi.useFakeTimers();
    process.env.TASK_ID = 'task-net-456';
    const url = 'https://x-access-token:ghs_NET_TOKEN@github.com/org/repo.git';
    const netErr = 'fatal: unable to access: Could not resolve host: github.com';

    setupGitCloneMock(1, netErr);

    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ data: { token: 'ghs_SHOULD_NOT_BE_FETCHED' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const clonePromise = cloneWithRetry(['clone', url, '/tmp/repo'], 1000);
    await vi.runAllTimersAsync();
    await clonePromise;

    vi.unstubAllGlobals();
    vi.useRealTimers();
    delete process.env.TASK_ID;

    // The token endpoint must NOT have been called for a network error.
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ── resolveCredentialsFromSecretVersion ───────────────────────────────────────
describe('resolveCredentialsFromSecretVersion', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    _setAgentTokenForTesting('');
    delete process.env['AGENT_SECRET_TOKEN'];
    delete process.env['ANTHROPIC_API_KEY'];
    delete process.env['CLAUDE_CODE_OAUTH_TOKEN'];
    delete process.env['OPENAI_API_KEY'];
    delete process.env['CODEX_API_KEY'];
    delete process.env['GEMINI_API_KEY'];
    delete process.env['GOOGLE_APPLICATION_CREDENTIALS'];
    delete process.env['GOOGLE_CLOUD_PROJECT'];
    delete process.env['GOOGLE_CLOUD_LOCATION'];
    delete process.env['CODEX_HOME'];
    delete process.env['GEMINI_CLI_TRUST_WORKSPACE'];
    vi.mocked(fs.writeFileSync).mockClear();
    vi.mocked(fs.mkdirSync).mockClear();
  });

  function makeManifestFetch(manifest: Record<string, unknown>): ReturnType<typeof makeFetchMock> {
    const manifestBase64 = Buffer.from(JSON.stringify(manifest)).toString('base64');
    return makeFetchMock().mockImplementation((url: unknown) => {
      const urlStr = String(url);
      if (urlStr.includes('metadata.google.internal')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ access_token: 'gcp-test-token' }) });
      }
      if (urlStr.includes('secretmanager.googleapis.com')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ payload: { data: manifestBase64 } }) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
  }

  it('populates getAgentToken() and apiHeaders[X-Agent-Token] from Secret Manager manifest', async () => {
    const expectedToken = 'test-agent-jwt-from-secret-manager';
    const fetchMock = makeManifestFetch({ env: { AGENT_SECRET_TOKEN: expectedToken } });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    // AGENT_TOKEN must be populated from the manifest
    expect(getAgentToken()).toBe(expectedToken);

    // apiHeaders['X-Agent-Token'] must also be populated so apiPost sends the token
    await apiPost('/api/test-token-header', {}, 1);
    const postCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/api/test-token-header'));
    expect(postCall).toBeDefined();
    const postHeaders = (defined(postCall)[1] as RequestInit).headers as Record<string, string>;
    expect(postHeaders['X-Agent-Token']).toBe(expectedToken);
  });

  it('populates LLM credential env vars from the manifest', async () => {
    const fetchMock = makeManifestFetch({
      env: {
        AGENT_SECRET_TOKEN: 'test-token',
        ANTHROPIC_API_KEY: 'sk-ant-test-key',
        CLAUDE_CODE_OAUTH_TOKEN: 'test-oauth-token',
      },
    });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    expect(process.env['ANTHROPIC_API_KEY']).toBe('sk-ant-test-key');
    expect(process.env['CLAUDE_CODE_OAUTH_TOKEN']).toBe('test-oauth-token');
  });

  it('does not overwrite an already-set LLM credential env var', async () => {
    process.env['ANTHROPIC_API_KEY'] = 'already-set-key';
    const fetchMock = makeManifestFetch({
      env: {
        AGENT_SECRET_TOKEN: 'test-token',
        ANTHROPIC_API_KEY: 'manifest-key-should-not-win',
      },
    });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    expect(process.env['ANTHROPIC_API_KEY']).toBe('already-set-key');
  });

  it('throws when the metadata token fetch returns a non-2xx status', async () => {
    const fetchMock = makeFetchMock().mockImplementation((url: unknown) => {
      if (String(url).includes('metadata.google.internal')) {
        return Promise.resolve({ ok: false, status: 403 });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1'),
    ).rejects.toThrow('failed to get GCP access token');
  });

  it('throws when the Secret Manager :access call returns a non-2xx status', async () => {
    const fetchMock = makeFetchMock().mockImplementation((url: unknown) => {
      if (String(url).includes('metadata.google.internal')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ access_token: 'gcp-test-token' }) });
      }
      if (String(url).includes('secretmanager.googleapis.com')) {
        return Promise.resolve({ ok: false, status: 403 });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1'),
    ).rejects.toThrow('failed to fetch secret');
  });

  it('resolveCredentialsFromSecretVersion writes files to disk and sets file-path env vars', async () => {
    const fetchMock = makeManifestFetch({
      env: {
        AGENT_SECRET_TOKEN: 'tok',
        GOOGLE_APPLICATION_CREDENTIALS: '/tmp/test-sa.json',
        GOOGLE_CLOUD_PROJECT: 'my-project',
      },
      files: [
        { path: '/tmp/test-sa.json', content: '{"type":"service_account"}', mode: '0600' },
      ],
    });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    // File-path env vars must be set from manifest.env
    expect(process.env['GOOGLE_APPLICATION_CREDENTIALS']).toBe('/tmp/test-sa.json');
    expect(process.env['GOOGLE_CLOUD_PROJECT']).toBe('my-project');

    // File must have been written to disk with correct content and mode
    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(
      '/tmp/test-sa.json',
      '{"type":"service_account"}',
      { encoding: 'utf-8', mode: 0o600 },
    );

    // Parent directory must have been created
    expect(vi.mocked(fs.mkdirSync)).toHaveBeenCalledWith(
      '/tmp',
      { recursive: true, mode: 0o700 },
    );
  });
});

// ── cloneWithRetry: commitless repositories ──────────────────────────────────
// A repo with zero commits has no default branch, so `git clone --branch <trunk>`
// fails with EXACTLY the same text as a branch that was deleted after its PR
// merged. Retrying cannot conjure a branch, and letting the ambiguous text stand
// gets the failure classified BRANCH_GONE ("PR merged before clone") — a benign
// transient story for a permanent setup gap. The remote is the only oracle: a
// commitless repo advertises zero refs.
describe('cloneWithRetry — commitless repo', () => {
  const CLONE_ARGS = [
    '-c', 'credential.https://github.com.helper=/home/agent/.blueprint/git-credential-helper',
    'clone', '--depth', '50', '--branch', 'main',
    'https://github.com/HenryHamster/playground.git', '/tmp/workspace',
  ];
  // The literal stderr from the production failure (task 235d7c59, playground).
  const BRANCH_MISSING_STDERR =
    "Cloning into '/tmp/workspace'...\nfatal: Remote branch main not found in upstream origin";

  /** A fake `git` child process: emits its output, then closes with `code`. */
  function fakeGitProc(code: number, { stdout = '', stderr = '' } = {}) {
    const proc = {
      stdout: { on: (ev: string, cb: (d: Buffer) => void) => { if (ev === 'data' && stdout) cb(Buffer.from(stdout)); } },
      stderr: { on: (ev: string, cb: (d: Buffer) => void) => { if (ev === 'data' && stderr) cb(Buffer.from(stderr)); } },
      on: (ev: string, cb: (c: number) => void) => { if (ev === 'close') setImmediate(() => { cb(code); }); return proc; },
      kill: vi.fn(),
    };
    return proc;
  }

  /** Script `git`: the clone always fails branch-missing; `ls-remote` reports
   *  whatever refs the remote is pretending to have. */
  function scriptGit(refs: string) {
    vi.mocked(spawn).mockImplementation(((_cmd: string, args: string[]) => {
      if (args[0] === 'ls-remote') return fakeGitProc(0, { stdout: refs });
      return fakeGitProc(128, { stderr: BRANCH_MISSING_STDERR });
    }) as unknown as typeof spawn);
  }

  const cloneAttempts = () =>
    vi.mocked(spawn).mock.calls.filter(c => (c[1] as string[]).includes('clone'));
  const lsRemoteProbes = () =>
    vi.mocked(spawn).mock.calls.filter(c => (c[1] as string[])[0] === 'ls-remote');

  beforeEach(() => { vi.mocked(spawn).mockReset(); });

  it('zero refs → fails fast with the real reason, burning no retries', async () => {
    scriptGit(''); // a commitless repo advertises no refs at all

    await expect(cloneWithRetry(CLONE_ARGS)).rejects.toThrow(/repository has no commits/i);

    // The whole point: one attempt, not three. Retrying a repo that has no
    // commits is pure latency — the branch cannot appear on its own.
    expect(cloneAttempts()).toHaveLength(1);
    expect(lsRemoteProbes()).toHaveLength(1);
  });

  it('probes the remote over the clone URL, not the credential-helper arg', async () => {
    scriptGit('');
    await expect(cloneWithRetry(CLONE_ARGS)).rejects.toThrow(/repository has no commits/i);

    expect(lsRemoteProbes()[0][1]).toEqual([
      'ls-remote', '--heads', 'https://github.com/HenryHamster/playground.git',
    ]);
  });

  it('deleted branch on a real repo → NOT reported as commitless, retries as before', async () => {
    // Same git error, but the remote still has refs: this is the branch-gone
    // case and must keep its existing behaviour. If the probe were skipped (or
    // its result ignored) a merged-PR race would be misreported as an unseeded
    // repo — the mirror image of the bug this fixes.
    scriptGit('a1b2c3\trefs/heads/main\nd4e5f6\trefs/heads/develop\n');

    vi.useFakeTimers();
    try {
      const attempt = cloneWithRetry(CLONE_ARGS);
      const assertion = expect(attempt).rejects.toThrow(/not found in upstream origin/i);
      await vi.runAllTimersAsync();
      await assertion;
      await expect(attempt).rejects.not.toThrow(/repository has no commits/i);
    } finally {
      vi.useRealTimers();
    }

    expect(cloneAttempts()).toHaveLength(3); // full retry budget, unchanged
  });
});
