import { spawn, execFileSync } from 'child_process';
import { fileURLToPath } from 'url';
import fs from 'fs';
import path from 'path';
import os from 'os';
import WebSocket from 'ws';
import { getLastWorkspacePrepOutcome, resetWorkspacePrepOutcome } from './workspacePrep.js';
import { handlers, resolveHandler } from './handlers/index.js';
import {
  loadTaskCapabilityContract,
  isReadOnlyTaskType,
  type TaskCapabilityContract,
} from './taskCapabilities.js';
import { extractJsonObject, extractJsonArray, JsonExtractionError } from './json-extract.js';
import {
  startEventLoopLagMonitor, traceHeartbeat, installExitMarkers, recordFinalState,
} from './diagnostics.js';
import { registerChildPid, unregisterChildPid } from './processTracking.js';
import { resetForNextRole, clearStaleProviderCredentials, TMP_OUTPUT_NAMES } from './reset.js';
import { isHeartbeatTransient } from './heartbeat-classify.js';
import {
  type FixLoopEvent,
  pushFixLoopEvent,
  drainFixLoopEventBuffer,
  requeueFixLoopEvents,
  resetCheckSynthesizerState,
  synthesizeCheckFixLoopEvents,
} from './fix-loop-events.js';
import {
  API_TIMEOUT, API_MAX_RETRIES, LOGS_MAX_LENGTH, API_RETRY_BACKOFF_MAX_MS,
  PROCESS_KILL_GRACE_MS,
  HEARTBEAT_INTERVAL, MAX_LOG_BUFFER, HEARTBEAT_INFLIGHT_DEADLINE,
  HEARTBEAT_API_TIMEOUT, HEARTBEAT_API_MAX_RETRIES,
  GIT_CLONE_TIMEOUT, CLONE_MAX_RETRIES, CLONE_RETRY_BASE_DELAY,
  CONFLICT_CHECK_TIMEOUT,
} from './constants.js';
import {
  initTokenModule, tryRefreshToken,
  scheduleProactiveTokenRefresh, stopProactiveTokenRefresh,
  scheduleGithubTokenRefresh, stopGithubTokenRefresh,
  computeRefreshDelayMs, isBootstrapToken,
} from './utils/agent-token.js';
import { ensureAgentTokenFileEnv, writeAgentTokenFile, ensureGithubTokenFileEnv, writeGithubTokenFile } from './token-file.js';
import {
  writeGitTokenFile,
  setupGitCredentialHelper,
  ensureGitCredentialHelperScript,
} from './git-credential.js';
import { sanitize, sanitizeError } from './utils/sanitize.js';
import {
  type StaleResultEnvelope,
  createStaleResultEnvelope, parseStaleResultFile,
  staleResultBackoffMs, shouldAbandonStaleResult, markStaleResultFailure,
} from './utils/stale-result.js';
import {
  detectLoopDivergence,
} from './utils/diff-detection.js';
import { prepareTaskWorkBranch } from './task-checkpoint.js';
export {
  buildTaskCheckpointCommitMessage,
  parseTaskCheckpointCommitMessage,
  prepareTaskWorkBranch,
} from './task-checkpoint.js';
import {
  type BuildConfig, type TestRunner,
  detectTestRunners,
  parseFailingTests, normalizePytestSelectorForRunner,
  extractPytestSelectorsFromOutput, buildTargetedPytestRunners,
} from './utils/test-runners.js';
import {
  PROVIDER, AUTH_MODE, LLM_EXECUTION_MODE, LLM_CLI_TRANSLATION,
  parseLLMCliTranslation, matchesRateLimit, matchesAuthFailure, matchesVmConfigMissing,
  RateLimitError, AuthFailureError, NoProgressTimeoutError, VmConfigMissingError,
  RuntimeUnavailableError,
  missingProviderConfigFiles, requiredProviderConfigFiles,
  refreshRuntimeState,
  getProviderRunner, providerSupportsNativeSubagents,
  __setRunClaudeCodeOverrideForSmoke, __setCommandExistsOverrideForSmoke, codexStreamHasTurnCompleted,
  runClaudeCode, runProviderAgent,
  getLastProviderRunErrorReport, clearLastProviderRunErrorReport,
  type LLMCliTranslation, type ClaudeOutput, type RunnerCommand,
  type ValidationCommand, type ProviderRunner, type ProviderRunErrorReport,
} from './providers/runners.js';

export {
  PROVIDER, AUTH_MODE, LLM_EXECUTION_MODE, LLM_CLI_TRANSLATION,
  parseLLMCliTranslation, matchesRateLimit, matchesAuthFailure, matchesVmConfigMissing,
  RateLimitError, AuthFailureError, NoProgressTimeoutError, VmConfigMissingError, RuntimeUnavailableError,
  missingProviderConfigFiles, requiredProviderConfigFiles,
  refreshRuntimeState,
  getProviderRunner, providerSupportsNativeSubagents,
  __setRunClaudeCodeOverrideForSmoke, __setCommandExistsOverrideForSmoke, codexStreamHasTurnCompleted,
  runClaudeCode, runProviderAgent,
  getLastProviderRunErrorReport, clearLastProviderRunErrorReport,
};
export type { LLMCliTranslation, ClaudeOutput, RunnerCommand, ValidationCommand, ProviderRunner, ProviderRunErrorReport };
export { computeRefreshDelayMs, isBootstrapToken };

// ── Failure categories ───────────────────────────────────────────────────────
export type FailureCategory = 'clone_failed' | 'build_failed' | 'test_failed' | 'lint_failed' | 'pr_creation_failed' | 'claude_code_failed' | 'parse_failed' | 'fatal' | 'rate_limited' | 'no_progress_timeout' | 'timeout' | 'oauth_rotation_failed' | 'reset_failed' | 'reassigned' | 'external_api' | 'vm_config_missing' | 'github_token_fetch_failed' | 'runtime_unavailable';

// ── Graceful-shutdown signalling (cluster C7) ────────────────────────────────
// A non-error "shutdown requested" control signal. Thrown from the deep call
// paths that previously did `setTimeout(process.exit); return` — the
// fall-through return raced main()'s role loop (which would re-enter
// pollForNextRole before the deferred exit fired) and skipped the terminal
// status POST. Throwing instead unwinds the stack cleanly back to main()'s
// role loop, which catches it, breaks, and lets the outer finally tear down
// the workspace before a single explicit exit. Distinct from Error so the
// global crash handler / runRoleOnce catch never mis-records it as a fault.
export class GracefulShutdownSignal {
  readonly reason: string;
  readonly exitCode: number;
  constructor(reason: string, exitCode = 0) {
    this.reason = reason;
    this.exitCode = exitCode;
  }
}

// Set once a non-transient heartbeat 4xx (reassignment/deauth) is observed.
// Read by the heartbeat loop so any in-flight tick that has already passed the
// classification gate does not keep firing after we have decided to shut down.
//
// WARM-VM ISOLATION: this is per-role state. On a warm VM (one process, many
// sequential roles) it MUST start false for each new role — otherwise a future
// code path that sets it without an immediate process.exit would leak the
// "shutting down" signal into the next role and silently suppress its
// heartbeats. Today the only setter (the heartbeat 4xx path) exits 5s later, so
// the leak is latent, but the warm-handoff reset (resetPerRoleModuleState)
// clears it unconditionally so the invariant holds regardless of future setters.
let cancelRequested = false; // @per-role
function requestCancel(): void { cancelRequested = true; }
function resetCancelRequested(): void { cancelRequested = false; }
export function _isCancelRequestedForTesting(): boolean { return cancelRequested; }
export function _resetCancelRequestedForTesting(): void { cancelRequested = false; }
export function _setCancelRequestedForTesting(): void { requestCancel(); }

// Crash-report task identity, decoupled from process.env.TASK_ID. The role
// loop deliberately clears TASK_ID during the warm-poll window so a benign
// SIGTERM (VM being torn down with no follow-up work) does not overwrite the
// prior task's status with a FATAL message. But that also stripped crash
// context from *real* crashes (uncaughtException / unhandledRejection) that
// happen during the poll gap — they reported with no task id and were silently
// lost. This module-level id is set at EVERY TASK_ID assignment and is NOT
// cleared during the poll window, so _crashReport can still attribute a
// poll-gap crash to the warm-pinned task (labelled distinctly so the backend
// can tell a genuine crash from the prior task's completed work).
let _crashReportTaskId = '';
// True while the role loop is in the warm-poll gap (TASK_ID intentionally
// blank). Lets _crashReport label poll-gap crashes distinctly.
let _inWarmPollWindow = false;
function setCrashReportTaskId(taskId: string): void { _crashReportTaskId = taskId; }
export function _getCrashReportTaskIdForTesting(): string { return _crashReportTaskId; }
export function _setInWarmPollWindowForTesting(value: boolean): void { _inWarmPollWindow = value; }
export function _crashReportForTesting(label: string, detail: string, exitCode: number): void { _crashReport(label, detail, exitCode); }

// ── OAuth credential rotation + ClaudeTimeoutError ───────────────────────────
import {
  recordInitialOAuthAuthHash,
  rotateOAuthCredentialsIfChanged as _rotateOAuthCredentialsIfChanged,
  ClaudeTimeoutError,
} from './oauth-rotation.js';
import { clearTestConventionCache, clearFileTreeCache } from './prompts/discovery.js';
export { recordInitialOAuthAuthHash, ClaudeTimeoutError };

/** Rotate OAuth credentials if changed, bound to the local apiPost. */
export function rotateOAuthCredentialsIfChanged(taskId: string): Promise<void> {
  return _rotateOAuthCredentialsIfChanged(taskId, apiPost).then(() => {
    if (activeCredentialStateSource === 'boot' && bootCredentialSnapshot !== null) {
      bootCredentialSnapshot = snapshotManagedCredentialState();
    }
  });
}

async function persistOAuthRotationAfterProviderValidation(
  taskId: string,
  reportPreClaimFailure: (reason: string) => void,
): Promise<void> {
  try {
    await rotateOAuthCredentialsIfChanged(taskId);
  } catch (rotationErr) {
    const reason = `oauth credential rotation failed after provider validation: ${
      rotationErr instanceof Error ? rotationErr.message : String(rotationErr)
    }`;
    log(reason);
    reportPreClaimFailure(reason);
    throw rotationErr;
  }
}

// ── Build configuration (from project settings, passed via claim response) ───
export type { DirectoryConfig, MigrationConfig } from './utils/test-runners.js';
export type { BuildConfig };

// ── Environment ─────────────────────────────────────────────────────────────
const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
let AGENT_TOKEN = process.env.AGENT_SECRET_TOKEN || '';
const VM_INSTANCE_ID = process.env.VM_INSTANCE_ID || 'local';
export const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';
// Root of the git object cache pre-baked into the agent VM image (see
// infra/packer/agent.pkr.hcl). A bare mirror lives at
// <root>/<owner>/<repo>.git; setupWorkspace passes it via
// `git clone --reference-if-able` so cold VMs reuse pre-fetched objects
// instead of re-downloading the full target repo every task.
export const GIT_REFERENCE_CACHE_ROOT = process.env.GIT_REFERENCE_CACHE_ROOT || '/opt/agent-cache/git';

// Populated at startup by fetchVmImageIdentity(); stays null in local dev /
// tests where the GCP metadata service is unreachable so vm-heartbeat sends
// a null vm_image_name (rather than 'unknown', which would pollute
// compute._vm_images and skew get_stale_image_vms accounting).
let vmImageName: string | null = null;
let vmImageTs: number | null = null;
// Content-hash fingerprint of /opt/agent/src baked into the image by packer
// (see infra/packer/agent.pkr.hcl). Read once at startup and included in
// vm-heartbeats and task heartbeats so stale-image diagnostics can identify
// the exact agent runtime revision independently of image name or build timestamp.
let agentRuntimeFingerprint: string | null = null;
// Git commit the image was built from, baked into /opt/agent/AGENT_SOURCE_SHA by
// packer (see infra/packer/agent.pkr.hcl). Read once at startup and included in
// vm-heartbeats and task heartbeats so a running VM maps back to its exact build
// commit. Stays null in local dev / on pre-marker images / on an 'unknown' bake.
let agentSourceSha: string | null = null;

/** Fetch the GCP boot-disk image path from the instance metadata service.
 *  Returns the short image name and extracted unix build timestamp, or
 *  falls back to { image_name: null, image_ts: null } on any error so
 *  callers can distinguish "unknown VM" from a real image. */
export async function fetchVmImageIdentity(): Promise<{ image_name: string | null; image_ts: number | null }> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => { controller.abort(); }, 2000);
    const res = await fetch(
      'http://metadata.google.internal/computeMetadata/v1/instance/image',
      { headers: { 'Metadata-Flavor': 'Google' }, signal: controller.signal },
    );
    clearTimeout(timer);
    if (!res.ok) return { image_name: null, image_ts: null };
    const path = (await res.text()).trim();
    // path = "projects/<proj>/global/images/blueprint-agent-v2-1730000000"
    const name = path.split('/').pop() ?? null;
    if (!name) return { image_name: null, image_ts: null };
    const tsMatch = /[-_](\d{9,})$/.exec(name);
    const image_ts = tsMatch ? parseInt(tsMatch[1], 10) : null;
    return { image_name: name, image_ts };
  } catch {
    return { image_name: null, image_ts: null };
  }
}

/**
 * When running in Cloud Run Job mode the backend does not inject the agent JWT
 * or LLM credential values as plaintext env-var overrides (they would be
 * visible in Cloud Audit Logs and run.executions.get responses). Instead the
 * backend passes AGENT_SECRET_VERSION — a Secret Manager version URI — and
 * this function fetches the manifest at that version, then populates
 * AGENT_TOKEN, all non-identity env vars, and any file credentials from it.
 *
 * Throws on any failure so the caller can surface a pre-claim heartbeat before
 * exiting.
 */
export async function resolveCredentialsFromSecretVersion(
  secretVersionName: string,
): Promise<void> {
  // 1. Obtain a GCP access token from the instance metadata service.
  const tokenController = new AbortController();
  const tokenTimer = setTimeout(() => { tokenController.abort(); }, 5000);
  let accessToken: string;
  try {
    const tokenRes = await fetch(
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
      { headers: { 'Metadata-Flavor': 'Google' }, signal: tokenController.signal },
    );
    clearTimeout(tokenTimer);
    if (!tokenRes.ok) {
      throw new Error(`metadata token fetch failed: HTTP ${tokenRes.status}`);
    }
    const tokenData = await tokenRes.json() as { access_token: string };
    accessToken = tokenData.access_token;
  } catch (err) {
    clearTimeout(tokenTimer);
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`resolveCredentialsFromSecretVersion: failed to get GCP access token: ${msg}`);
  }

  // 2. Access the secret version via the Secret Manager REST API.
  const smController = new AbortController();
  const smTimer = setTimeout(() => { smController.abort(); }, 10000);
  let manifestJson: string;
  try {
    const smRes = await fetch(
      `https://secretmanager.googleapis.com/v1/${secretVersionName}:access`,
      {
        headers: { Authorization: `Bearer ${accessToken}` },
        signal: smController.signal,
      },
    );
    clearTimeout(smTimer);
    if (!smRes.ok) {
      throw new Error(`Secret Manager access failed: HTTP ${smRes.status}`);
    }
    const smData = await smRes.json() as { payload: { data: string } };
    manifestJson = Buffer.from(smData.payload.data, 'base64').toString('utf-8');
  } catch (err) {
    clearTimeout(smTimer);
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`resolveCredentialsFromSecretVersion: failed to fetch secret: ${msg}`);
  }

  // 3. Populate AGENT_TOKEN, all non-identity env vars, and file credentials from the manifest.
  const manifest = JSON.parse(manifestJson) as {
    env?: Record<string, string>;
    files?: Array<{ path: string; content: string; mode?: string | number }>;
  };
  const env = manifest.env ?? {};
  if (!AGENT_TOKEN && env['AGENT_SECRET_TOKEN']) {
    AGENT_TOKEN = env['AGENT_SECRET_TOKEN'];
    process.env['AGENT_SECRET_TOKEN'] = env['AGENT_SECRET_TOKEN'];
    apiHeaders['X-Agent-Token'] = AGENT_TOKEN;
  }
  for (const [key, value] of Object.entries(env)) {
    if (!IDENTITY_ENV_KEYS.has(key) && value && !process.env[key]) {
      process.env[key] = value;
    }
  }
  for (const file of manifest.files ?? []) {
    ensureParentDirectorySync(file.path);
    writePrivateFileSync(file.path, file.content, parseSecretFileMode(file.mode));
  }
}

/** Read the baked agent-runtime fingerprint from the well-known path written
 *  by the packer provisioner. Returns null in local dev / test environments
 *  where the file is absent so callers can distinguish "no fingerprint" from
 *  a real hash. */
export function readAgentRuntimeFingerprint(): string | null {
  try {
    const raw = fs.readFileSync('/opt/agent/AGENT_RUNTIME_FINGERPRINT', 'utf-8').trim();
    const m = /^AGENT_RUNTIME_FINGERPRINT=([0-9a-f]{64})$/.exec(raw);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

/** Read the baked source git SHA from the well-known marker written by the
 *  packer provisioner (/opt/agent/AGENT_SOURCE_SHA). Returns the commit SHA, or
 *  null when the file is absent (local dev / pre-marker image) or carries an
 *  empty/'unknown' value (a build with no commit in the environment) — so the
 *  agent never reports a bogus SHA over the wire. The permissive 7-40 hex match
 *  also rejects the literal 'unknown' fallback. */
export function readAgentSourceSha(): string | null {
  try {
    const raw = fs.readFileSync('/opt/agent/AGENT_SOURCE_SHA', 'utf-8').trim();
    const m = /^AGENT_SOURCE_SHA=([0-9a-f]{7,40})$/.exec(raw);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

/** Get the current agent token (may have been refreshed since startup). */
export function getAgentToken(): string { return AGENT_TOKEN; }

/** Test-only: override the live agent token used by main()/api calls. */
export function _setAgentTokenForTesting(value: string): void {
  AGENT_TOKEN = value;
  apiHeaders['X-Agent-Token'] = AGENT_TOKEN;
}

const apiHeaders: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Agent-Token': AGENT_TOKEN,
};

initTokenModule({
  getOrchestratorUrl: () => ORCHESTRATOR_URL,
  getHeaders: () => ({ ...apiHeaders }),
  getToken: () => AGENT_TOKEN,
  setToken: (newToken: string) => {
    AGENT_TOKEN = newToken;
    apiHeaders['X-Agent-Token'] = AGENT_TOKEN;
    // Mirror the refreshed token to the live token file so the long-lived
    // resident session's bash helpers (./strategic, ./messages, ...) read the
    // ROTATED token on their next invocation instead of the stale spawn-time
    // AGENT_API_TOKEN env snapshot. Best-effort + no-op unless the file is
    // configured (agent startup); never throws, never logs the token.
    writeAgentTokenFile(AGENT_TOKEN);
  },
  log,
});

// ── Helpers ──────────────────────────────────────────────────────────────────
export function strVal(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

/**
 * Resolve a task's base branch through the per-project cascade:
 * ``payload.base_branch`` → ``task.project_metadata.integration_branch`` → ``fallback``.
 *
 * The backend stamps every claim response's ``project_metadata.integration_branch``
 * (see backend/app/routers/agent.py), so any handler that defaults to ``'main'``
 * without consulting that field silently breaks projects whose integration branch
 * is something else (e.g. ``master``, ``production``, ``develop``). Use this helper
 * everywhere a handler needs the project's integration branch.
 */
export function resolveBaseBranch(
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  fallback = 'main',
): string {
  const fromPayload = strVal(payload.base_branch).trim();
  if (fromPayload) return fromPayload;
  const meta = task.project_metadata as Record<string, unknown> | undefined;
  const fromMeta = strVal(meta?.integration_branch).trim();
  if (fromMeta) return fromMeta;
  return fallback;
}


function writePrivateFileSync(path: string, contents: string, mode = 0o600): void {
  fs.writeFileSync(path, contents, { encoding: 'utf-8', mode });
  try { fs.chmodSync(path, mode); } catch { /* best effort on non-POSIX filesystems */ }
}

interface SecretManifestFile {
  path: string;
  content: string;
  mode: number;
}

interface MaterializedCredentialState {
  env: Record<string, string>;
  files: SecretManifestFile[];
}

const MANAGED_LLM_ENV_KEYS = [
  'PROVIDER',
  'AUTH_MODE',
  'LLM_MODEL',
  'LLM_EXECUTION_MODE',
  'LLM_SETTINGS',
  'LLM_CLI_TRANSLATION',
  'CLAUDE_CODE_OAUTH_TOKEN',
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'ANTHROPIC_BASE_URL',
  'ZAI_API_KEY',
  'ZAI_ANTHROPIC_BASE_URL',
  'ZAI_BASE_URL',
  'DEEPSEEK_API_KEY',
  'DEEPSEEK_ANTHROPIC_BASE_URL',
  'DEEPSEEK_BASE_URL',
  // Claude Code model-selection env materialized by the DeepSeek secret
  // material. Only the deepseek branch of buildBaseCliEnv reads these, but
  // they are provider credentials-adjacent state and must be cleared (and
  // snapshotted/restored) across warm retargets like every other managed
  // key — otherwise a deepseek→anthropic retarget leaves stale DeepSeek
  // model ids in process.env forever.
  'ANTHROPIC_MODEL',
  'ANTHROPIC_DEFAULT_OPUS_MODEL',
  'ANTHROPIC_DEFAULT_SONNET_MODEL',
  'ANTHROPIC_DEFAULT_HAIKU_MODEL',
  'CLAUDE_CODE_SUBAGENT_MODEL',
  'OPENAI_API_KEY',
  'CODEX_API_KEY',
  'CODEX_HOME',
  'GEMINI_API_KEY',
  'GOOGLE_APPLICATION_CREDENTIALS',
  'GOOGLE_CLOUD_PROJECT',
  'GOOGLE_CLOUD_LOCATION',
  'GOOGLE_GENAI_USE_VERTEXAI',
  'GEMINI_CLI_TRUST_WORKSPACE',
];
const IDENTITY_ENV_KEYS = new Set([
  'AGENT_SECRET_TOKEN',
  'TASK_ID',
  'VM_INSTANCE_ID',
  'ORCHESTRATOR_URL',
  'PROJECT_ID',
  'BLUEPRINT_POOL_MODE',
]);
// Resolve the agent's HOME exactly the way the spawned provider CLI does
// (runners.ts:buildBaseCliEnv → `process.env.HOME || os.homedir()`), so every
// credential/config file we materialize lands where the CLI actually reads it.
// On the cloud VM HOME=/home/agent → reproduces the historical hardcoded paths;
// inside the self-hosted fleet-runner bwrap sandbox HOME=/home (fresh tmpfs, NO
// host $HOME bind) → creds materialize under /home. Read at CALL time (never
// cached) so it tracks a late-set HOME and so tests that stub process.env.HOME
// observe the right paths.
function agentHome(): string {
  return process.env.HOME || os.homedir();
}

// Managed provider credential/config files fall under two anchoring regimes.
//
// (1) $HOME-relative — the provider CLI reads these from $HOME, so the WRITE
//     path MUST track the live HOME. Held as canonical TAILS so a backend-
//     supplied absolute path (baked for the cloud-VM image, e.g.
//     /home/agent/.claude.json) can be re-anchored onto the agent's actual
//     $HOME (resolveManagedCredentialWritePath) without hardcoding /home/agent:
//       ~/.claude.json          — Claude Code onboarding-bypass file (the CLI
//                                 dies in seconds without it)
//       ~/.gemini/settings.json — Gemini CLI custom-alias settings
const HOME_ANCHORED_CRED_SUBPATHS = ['.claude.json', '.gemini/settings.json'] as const;

// (2) install-dir-anchored — Codex and Gemini credentials are pointed to by
//     environment variables. Cloud images use /opt/agent, while a local bwrap
//     runner relocates them into its writable /workspace mount. Resolve the
//     live env paths so snapshots and OAuth rotation follow the same files the
//     provider CLIs actually use.
function installDirAnchoredCredPaths(): string[] {
  const codexHome = process.env.CODEX_HOME?.trim().replace(/\/+$/, '');
  const googleCredentials = process.env.GOOGLE_APPLICATION_CREDENTIALS?.trim();
  return [
    codexHome ? `${codexHome}/auth.json` : '/opt/agent/openai/auth.json',
    googleCredentials || '/opt/agent/gemini/sa.json',
  ];
}

function knownManagedCredentialFilePaths(): string[] {
  const home = agentHome();
  return [
    ...HOME_ANCHORED_CRED_SUBPATHS.map(sub => `${home}/${sub}`),
    ...installDirAnchoredCredPaths(),
  ];
}

// Map a backend-supplied managed-cred file path onto the agent's ACTUAL $HOME.
// The backend manifest bakes absolute paths for the cloud-VM image
// (HOME=/home/agent), but the provider CLI reads HOME-relative creds from the
// LIVE $HOME — which is /home inside the fleet-runner bwrap sandbox (fresh
// tmpfs, NO host $HOME bind). A HOME-relative managed cred (matched by canonical
// tail) is re-anchored under agentHome(); every other path — including the
// install-dir-anchored /opt/agent creds — is returned unchanged. On the cloud
// VM this is a no-op (HOME=/home/agent → identical paths); in the sandbox it
// redirects the write to where the CLI actually reads.
function resolveManagedCredentialWritePath(manifestPath: string): string {
  const home = agentHome();
  for (const sub of HOME_ANCHORED_CRED_SUBPATHS) {
    if (manifestPath.endsWith(`/${sub}`) || manifestPath === `${home}/${sub}`) {
      return `${home}/${sub}`;
    }
  }
  return manifestPath;
}

// Older runners may still expose read-only /opt/agent credential paths, so
// materialization remains best-effort (log + continue) for compatibility.
// Current bwrap runners relocate the pointing env vars and files together into
// /workspace before starting the agent.
function isReadOnlyInstallDirCredPath(writePath: string): boolean {
  return writePath.startsWith('/opt/agent/');
}

let bootCredentialSnapshot: MaterializedCredentialState | null = null;
let lastMaterializedCredentialPaths = new Set<string>();
let activeCredentialStateSource: 'boot' | 'retarget' = 'boot';
// Which managed provider config files existed on disk when this PROCESS
// started — captured at the top of main(), BEFORE provider validation or any
// claim work can run. This is the "what did boot actually materialize?"
// baseline the vm_config_missing guards compare against: claude_config_json is
// an OPTIONAL per-account pool field, so a path like ~/.claude.json that was
// NEVER materialized on this VM is a healthy state (the CLI runs on env/OAuth
// alone), while a path that existed here and is now gone is a genuinely
// poisoned VM. null until captured (local dev / unit tests) — the guards
// treat null as "no expectations", never as "everything is missing".
let bootProviderConfigPathsPresent: Set<string> | null = null;

function parseSecretFileMode(raw: unknown): number {
  if (typeof raw === 'number' && Number.isInteger(raw) && raw > 0) return raw;
  if (typeof raw === 'string' && /^[0-7]{3,4}$/.test(raw)) return parseInt(raw, 8);
  return 0o600;
}

function normalizeClaudeConfigContent(
  filePath: string,
  content: string,
  workspace = WORKSPACE,
): string {
  if (path.basename(filePath) !== '.claude.json') return content;
  try {
    const parsed: unknown = JSON.parse(content || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return content;
    }
    const normalizedConfig = parsed as Record<string, unknown>;
    normalizedConfig.hasCompletedOnboarding = true;
    const projects = normalizedConfig.projects;
    const normalizedProjects =
      projects && typeof projects === 'object' && !Array.isArray(projects)
        ? projects as Record<string, unknown>
        : {};
    normalizedConfig.projects = normalizedProjects;
    const workspaceConfig = normalizedProjects[workspace];
    const normalizedWorkspace =
      workspaceConfig &&
      typeof workspaceConfig === 'object' &&
      !Array.isArray(workspaceConfig)
        ? workspaceConfig as Record<string, unknown>
        : {};
    normalizedProjects[workspace] = normalizedWorkspace;
    normalizedWorkspace.hasTrustDialogAccepted = true;
    // Claude Code 2.1.197 iterates projectSettings.allowedTools without
    // guarding the legacy/missing shape. Account exports created before that
    // field existed (or our trust bootstrap's newly-created workspace entry)
    // otherwise crash before the first model call with:
    //   TypeError: B.allowedTools is not iterable
    // Preserve a real list, but replace every absent or malformed value with
    // the least-privilege empty list. Runtime task permissions still arrive
    // through --disallowedTools / --dangerously-skip-permissions.
    if (!Array.isArray(normalizedWorkspace.allowedTools)) {
      normalizedWorkspace.allowedTools = [];
    }
    return JSON.stringify(normalizedConfig);
  } catch {
    return content;
  }
}

function ensureParentDirectorySync(filePath: string): void {
  const parent = path.dirname(filePath);
  fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
  try { fs.chmodSync(parent, 0o700); } catch { /* best effort on non-POSIX filesystems */ }
}

function clearManagedCredentialState(): void {
  for (const key of MANAGED_LLM_ENV_KEYS) {
    Reflect.deleteProperty(process.env, key);
  }
  const paths = new Set<string>([
    ...knownManagedCredentialFilePaths(),
    ...lastMaterializedCredentialPaths,
  ]);
  for (const filePath of paths) {
    try { fs.unlinkSync(filePath); } catch { /* missing files are fine */ }
  }
  lastMaterializedCredentialPaths = new Set();
}

function snapshotManagedCredentialState(): MaterializedCredentialState {
  const env: Record<string, string> = {};
  for (const key of MANAGED_LLM_ENV_KEYS) {
    const value = process.env[key];
    if (value !== undefined) env[key] = value;
  }
  const files: SecretManifestFile[] = [];
  for (const filePath of knownManagedCredentialFilePaths()) {
    try {
      const content = fs.readFileSync(filePath, 'utf-8');
      files.push({ path: filePath, content, mode: 0o600 });
    } catch { /* absent files are normal for other providers */ }
  }
  return { env, files };
}

function materializeCredentialState(
  state: MaterializedCredentialState,
  source: 'boot' | 'retarget',
): void {
  clearManagedCredentialState();
  const materializedPaths: string[] = [];
  for (const file of state.files) {
    // Re-anchor HOME-relative managed creds (~/.claude.json, ~/.gemini/settings.json)
    // onto the LIVE $HOME so the write lands where the provider CLI reads it:
    // identical to today on the cloud VM (HOME=/home/agent), redirected under
    // /home inside the fleet-runner sandbox. Install-dir + non-managed paths are
    // written at their manifest path unchanged.
    const writePath = resolveManagedCredentialWritePath(file.path);
    if (isReadOnlyInstallDirCredPath(writePath)) {
      // /opt/agent is READ-ONLY inside the sandbox — this write may fail there.
      // Best-effort: the Claude path works without these, so a failure must not
      // sink the whole role. On the cloud VM the dir is agent-owned and the
      // write always succeeds.
      try {
        ensureParentDirectorySync(writePath);
        writePrivateFileSync(writePath, file.content, file.mode);
        materializedPaths.push(writePath);
      } catch (e) {
        log(
          `[cred] best-effort write of install-dir provider cred ${writePath} failed ` +
          `(expected under a read-only sandbox bind; openai/gemini need a coordinated ` +
          `backend relocation to run on a sandboxed runner): ` +
          (e instanceof Error ? e.message : String(e)),
        );
      }
      continue;
    }
    ensureParentDirectorySync(writePath);
    writePrivateFileSync(
      writePath,
      normalizeClaudeConfigContent(writePath, file.content),
      file.mode,
    );
    materializedPaths.push(writePath);
  }
  for (const [key, value] of Object.entries(state.env)) {
    process.env[key] = value;
  }
  lastMaterializedCredentialPaths = new Set(materializedPaths);
  activeCredentialStateSource = source;
  refreshRuntimeState();
  recordInitialOAuthAuthHash();
}

function toFailure(message: string): Error & { failureCategory: FailureCategory } {
  const err = new Error(message) as Error & { failureCategory: FailureCategory };
  err.failureCategory = 'reset_failed';
  return err;
}

function normalizeSecretManifest(manifest: unknown, context: string): MaterializedCredentialState {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw toFailure(`${context}: secret_manifest must be an object`);
  }
  const record = manifest as Record<string, unknown>;
  const metadata = record.metadata;
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
    throw toFailure(`${context}: secret_manifest.metadata must be an object`);
  }
  if ((metadata as Record<string, unknown>).retarget !== true) {
    throw toFailure(`${context}: secret_manifest.metadata.retarget must be true`);
  }
  for (const forbiddenKey of ['task_id', 'vm_instance_id', 'project_id', 'pool_mode']) {
    if (Object.prototype.hasOwnProperty.call(metadata, forbiddenKey)) {
      throw toFailure(`${context}: secret_manifest.metadata.${forbiddenKey} is not allowed`);
    }
  }
  const envRaw = record.env;
  if (!envRaw || typeof envRaw !== 'object' || Array.isArray(envRaw)) {
    throw toFailure(`${context}: secret_manifest.env must be an object`);
  }
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(envRaw as Record<string, unknown>)) {
    if (IDENTITY_ENV_KEYS.has(key)) {
      throw toFailure(`${context}: secret_manifest.env.${key} is not allowed`);
    }
    if (typeof value !== 'string') {
      throw toFailure(`${context}: secret_manifest.env.${key} must be a string`);
    }
    env[key] = value;
  }
  if (!env.PROVIDER || !env.AUTH_MODE) {
    throw toFailure(`${context}: secret_manifest must include PROVIDER and AUTH_MODE`);
  }
  const filesRaw = record.files;
  if (filesRaw !== undefined && !Array.isArray(filesRaw)) {
    throw toFailure(`${context}: secret_manifest.files must be an array when present`);
  }
  const files: SecretManifestFile[] = [];
  for (const [idx, item] of (filesRaw ?? []).entries()) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      throw toFailure(`${context}: secret_manifest.files[${idx}] must be an object`);
    }
    const file = item as Record<string, unknown>;
    if (typeof file.path !== 'string' || !path.isAbsolute(file.path)) {
      throw toFailure(`${context}: secret_manifest.files[${idx}].path must be an absolute path`);
    }
    if (typeof file.content !== 'string') {
      throw toFailure(`${context}: secret_manifest.files[${idx}].content must be a string`);
    }
    files.push({
      path: file.path,
      content: file.content,
      mode: parseSecretFileMode(file.mode),
    });
  }
  return { env, files };
}

function captureBootCredentialSnapshot(): void {
  if (bootCredentialSnapshot !== null) return;
  bootCredentialSnapshot = snapshotManagedCredentialState();
  lastMaterializedCredentialPaths = new Set(bootCredentialSnapshot.files.map(file => file.path));
  activeCredentialStateSource = 'boot';
}

// Record which managed provider config files exist on disk RIGHT NOW.
// Idempotent — first capture wins, so it records the state at process start
// even if a later code path calls it again. Candidates cover the known
// managed credential files plus whatever the boot provider hard-requires.
function captureBootProviderConfigPresence(): void {
  if (bootProviderConfigPathsPresent !== null) return;
  const candidates = new Set<string>([
    ...knownManagedCredentialFilePaths(),
    ...requiredProviderConfigFiles(PROVIDER),
  ]);
  bootProviderConfigPathsPresent = new Set([...candidates].filter(p => {
    try {
      return fs.existsSync(p);
    } catch {
      // A failed stat must not fabricate a "this file existed" expectation.
      return false;
    }
  }));
}

// The provider config files the CURRENT credential state is supposed to have
// on disk — the expected-set the vm_config_missing guards check against:
//   - after any in-process materialization (boot snapshot re-write or retarget
//     manifest), the just-materialized file set is the authority: exactly what
//     the active source promised to write, nothing more;
//   - before the first materialization (the pre-claim window), the boot
//     presence snapshot is: whatever the VM's provision-time boot
//     materialization actually put on disk.
// A required-by-provider path that is in NEITHER set never existed on this VM
// (claude_config_json is an optional per-account field) and must not be
// treated as lost.
function expectedProviderConfigPaths(): ReadonlySet<string> {
  if (bootCredentialSnapshot !== null || activeCredentialStateSource === 'retarget') {
    return lastMaterializedCredentialPaths;
  }
  return bootProviderConfigPathsPresent ?? new Set<string>();
}

// Poisoned-VM claim guard (2026-06-06 incidents, tasks 1330d291 / cf28097f):
// reports provider-required config files that were materialized on this VM
// (boot or retarget) but are now ABSENT — i.e. genuine "boot materialization
// lost". A file that never existed on this VM is NOT reported: the
// claude_config_json pool field is optional ("Not required for most setups"),
// so accounts without it boot perfectly healthy VMs that never have
// ~/.claude.json (the CLI runs on env/OAuth alone). Treating never-existed as
// lost terminally FAILED 49 tasks on 2026-06-06 (~19:42-20:58 UTC) — every
// fresh provision for such accounts died pre-claim across all 3 attempts.
// The disk check only applies on a real VM — local dev / unit tests never
// have /home/agent/.claude.json and must not be doomed for it. The
// orchestrator's startup script always bakes a real VM_INSTANCE_ID into
// /etc/agent.env; 'local' is the module-level fallback for dev machines.
export function missingVmProviderConfigFiles(provider = PROVIDER): string[] {
  const vmId = process.env.VM_INSTANCE_ID;
  if (!vmId || vmId === 'local') return [];
  const expected = expectedProviderConfigPaths();
  return missingProviderConfigFiles(provider).filter(p => expected.has(p));
}

// Refuse to hand a claimed role to a poisoned VM. Runs AFTER
// materializeClaimCredentialState has re-written every credential file it
// knows about (boot snapshot or retarget manifest) — that re-write IS the
// self-heal; if a config file the active source PROMISED (it is in the
// just-materialized expected-set) is still absent afterwards, no source on
// this VM can recreate it (the poisoned post-retarget state), so the only safe
// move is to fail the claim with the distinct vm_config_missing category. The
// orchestrator requeues the task as an infra failure (PROVISION_FAILURE) and
// cold-provisions instead of letting every claim die in seconds with
// "Claude configuration file not found". A file the active source never
// promised (account without the optional claude_config_json) is NOT a refusal
// condition — see missingVmProviderConfigFiles.
function assertVmProviderConfigPresent(context: string): void {
  const missing = missingVmProviderConfigFiles();
  if (missing.length === 0) return;
  const err = new Error(
    `${context}: vm_config_missing — materialized provider config absent after re-materialization: ${missing.join(', ')}; refusing to run a role configless`,
  ) as Error & { failureCategory: FailureCategory };
  err.failureCategory = 'vm_config_missing';
  throw err;
}

function materializeClaimCredentialState(
  secretManifest: unknown,
  context: string,
): void {
  if (secretManifest !== undefined) {
    materializeCredentialState(normalizeSecretManifest(secretManifest, context), 'retarget');
  } else {
    if (bootCredentialSnapshot === null) {
      throw toFailure(`${context}: boot credential snapshot is unavailable`);
    }
    materializeCredentialState(bootCredentialSnapshot, 'boot');
  }
  assertVmProviderConfigPresent(context);
}

export function _resetCredentialStateForTesting(): void {
  bootCredentialSnapshot = null;
  lastMaterializedCredentialPaths = new Set();
  activeCredentialStateSource = 'boot';
  bootProviderConfigPathsPresent = null;
  refreshRuntimeState();
}

export function _captureBootCredentialSnapshotForTesting(): void {
  captureBootCredentialSnapshot();
}

export function _captureBootProviderConfigPresenceForTesting(): void {
  captureBootProviderConfigPresence();
}

export function _materializeClaimCredentialStateForTesting(secretManifest?: unknown, context = 'test'): void {
  materializeClaimCredentialState(secretManifest, context);
}

export function _getActiveCredentialStateSourceForTesting(): 'boot' | 'retarget' {
  return activeCredentialStateSource;
}

/**
 * Shared retry/auth/backoff core for orchestrator HTTP calls.
 *
 * Owns: per-attempt AbortController/timeout, 401 token-refresh-once retry,
 * 429 Retry-After honoring, 4xx fail-fast, 5xx exponential backoff, and the
 * final "failed after N retries" surface.
 *
 * `init()` is invoked fresh per attempt so apiHeaders mutations from a
 * token refresh land on the next request without re-plumbing.
 *
 * `responseInterceptor` lets POST inject the "/api/agent/status 401 → task
 * already terminal" success short-circuit that GET has no equivalent for.
 * Returning a non-undefined value short-circuits with that value; throwing
 * propagates through the outer catch where 4xx errors bail out of the loop.
 */
type ResponseInterceptor = (status: number, text: string) => unknown;

export async function apiRequest(
  method: string,
  path: string,
  init: () => RequestInit,
  retries = API_MAX_RETRIES,
  timeoutMs = API_TIMEOUT,
  responseInterceptor?: ResponseInterceptor,
): Promise<unknown> {
  let tokenRefreshed = false;
  for (let attempt = 0; attempt < retries; attempt++) {
    // The abort timer must cover the response BODY read, not just the headers.
    // Clearing it the instant `fetch()` resolves (the prior behaviour) left
    // `await res.json()/res.text()` UNBOUNDED by our timeout: a server that
    // returns headers and then stalls the body hangs the request indefinitely
    // (undici's own body timeout is far longer, or never if bytes trickle). On
    // the heartbeat path such a hang never settled, so the `heartbeatInFlight`
    // mutex latched and silenced every subsequent liveness POST (rich heartbeat
    // AND off-row vmcontact ping) until the reaper false-killed a live VM as
    // AGENT_TIMEOUT (the 1474c03c failure mode). Re-arm a fresh body-phase timer
    // after headers and clear it in `finally` on every exit path, so the whole
    // request always settles (~2×timeoutMs worst case).
    const controller = new AbortController();
    let timeout = setTimeout(() => { controller.abort(); }, timeoutMs);
    try {
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, { ...init(), signal: controller.signal });
      clearTimeout(timeout);
      timeout = setTimeout(() => { controller.abort(); }, timeoutMs);
      if (res.ok) return await res.json() as unknown;
      const text = await res.text();

      if (responseInterceptor) {
        const intercepted = responseInterceptor(res.status, text);
        if (intercepted !== undefined) return intercepted;
      }

      // 401: attempt token refresh at most once, then retry
      if (res.status === 401) {
        if (tokenRefreshed) throw new Error(`${method} ${path} failed (401): ${text}`);
        log(`${method} ${path} got 401 — attempting token refresh`);
        const refreshed = await tryRefreshToken();
        if (!refreshed) throw new Error(`${method} ${path} failed (401): ${text}`);
        tokenRefreshed = true;
        continue;
      }
      // 429: retry after Retry-After header. Backend uses this for
      // temporary-contention signals (e.g. row-locked in /api/agent/status);
      // treating it as transient keeps the agent's retry loop aligned with
      // 5xx handling without tripping Cloud Run ContainerError classification.
      if (res.status === 429) {
        if (attempt === retries - 1) {
          throw new Error(`${method} ${path} failed (${res.status}): ${text}`);
        }
        const retryAfterSec = parseInt(res.headers.get('Retry-After') || '5', 10);
        const delayMs = Math.max(1000, Math.min(retryAfterSec * 1000, API_RETRY_BACKOFF_MAX_MS));
        log(`${method} ${path} got 429 (rate-limited/locked), retrying in ${delayMs}ms`);
        await new Promise(r => setTimeout(r, delayMs));
        continue;
      }
      // 503 (or a 403 carrying a Retry-After): temporary upstream unavailability
      // — e.g. the create-PR endpoint refusing with 503 + Retry-After while the
      // shared GitHub install-token quota is drained (it cannot confirm no
      // duplicate PR). Back off honoring Retry-After when present, else
      // exponential, capped like the 429 path. A 403 is retried here ONLY when
      // it carries a Retry-After, so genuine auth/permission 403s (no such
      // header) still fail fast via the 4xx bail-out below.
      //
      // Note: this path clamps Retry-After to API_RETRY_BACKOFF_MAX_MS (30s),
      // intentionally TIGHTER than the backend's advertised ceiling (up to 120s)
      // and the ./pr shell loop's 120s honor cap. This is a Node fetch retry with
      // a bounded per-request budget (not the synchronous ./pr step), so a short
      // cap keeps it responsive and simply re-polls sooner; the backend still
      // won't create until the quota returns, so an earlier retry is harmless.
      const retryAfterHeader = res.headers.get('Retry-After') ?? null;
      if (res.status === 503 || (res.status === 403 && retryAfterHeader !== null)) {
        if (attempt === retries - 1) {
          throw new Error(`${method} ${path} failed (${res.status}): ${text}`);
        }
        const retryAfterSec = retryAfterHeader !== null ? parseInt(retryAfterHeader, 10) : NaN;
        const delayMs = Number.isFinite(retryAfterSec) && retryAfterSec > 0
          ? Math.max(1000, Math.min(retryAfterSec * 1000, API_RETRY_BACKOFF_MAX_MS))
          : Math.min(1000 * Math.pow(2, attempt) + Math.random() * 1000, API_RETRY_BACKOFF_MAX_MS);
        log(`${method} ${path} got ${res.status} (temporary/upstream-unavailable), retrying in ${delayMs}ms`);
        await new Promise(r => setTimeout(r, delayMs));
        continue;
      }
      // Other 4xx errors are not transient — fail immediately
      if (res.status < 500) {
        throw new Error(`${method} ${path} failed (${res.status}): ${text}`);
      }
      // 5xx: retry if we have attempts left
      if (attempt === retries - 1) {
        throw new Error(`${method} ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      // Re-throw 4xx errors and final-attempt errors immediately. 429 is
      // intentionally excluded from the 4xx bail-out via the explicit check
      // above, which uses `continue` before this catch runs.
      if (attempt === retries - 1 || (e instanceof Error && /failed \(4\d{2}\)/.test(e.message))) throw e;
    } finally {
      // Clear whichever timer is currently armed (header- or body-phase) on
      // every exit: return, throw, 401/429 `continue`, or 5xx fall-through.
      clearTimeout(timeout);
    }
    await new Promise(r => setTimeout(r, Math.min(1000 * Math.pow(2, attempt) + Math.random() * 1000, API_RETRY_BACKOFF_MAX_MS)));
  }
  throw new Error(`${method} ${path} failed after ${retries} retries`);
}

export async function apiPost(path: string, body: Record<string, unknown> = {}, retries = API_MAX_RETRIES, timeoutMs = API_TIMEOUT) {
  // Cap logs field to avoid 422 from backend's max_length=500000 validation.
  // A 422 is a 4xx = no retry = agent dies silently.
  if (body.logs && typeof body.logs === 'string' && body.logs.length > LOGS_MAX_LENGTH) {
    body = { ...body, logs: body.logs.slice(0, LOGS_MAX_LENGTH) + '\n...(truncated)' };
  }
  return apiRequest(
    'POST', path,
    () => ({ method: 'POST', headers: apiHeaders, body: JSON.stringify(body) }),
    retries, timeoutMs,
    (status, text) => {
      // 401 with "no longer active": task completed/failed — our previous POST worked.
      // This happens when a status COMPLETED POST succeeded but the response was lost,
      // so the agent retries and hits JWT revocation. Treat as success for status path,
      // hard-fail (no retry) for everything else.
      if (status === 401 && text.includes('no longer active')) {
        if (path === '/api/agent/status') {
          log(`POST ${path} got 401 (task terminated) — previous POST likely succeeded`);
          return { status: 'already_terminal', task_id: body.task_id };
        }
        throw new Error(`POST ${path} failed (401): ${text}`);
      }
      return undefined;
    },
  );
}

export type { StaleResultEnvelope };
export { createStaleResultEnvelope, parseStaleResultFile, staleResultBackoffMs, shouldAbandonStaleResult, markStaleResultFailure };

type PersistedStaleResultEnvelope = StaleResultEnvelope & {
  agentToken?: string;
};

function decodeJwtSub(token: string): string | null {
  try {
    const parts = token.split('.');
    if (parts.length < 2) return null;
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = Buffer.from(b64, 'base64').toString('utf8');
    const payload = JSON.parse(json) as { sub?: unknown };
    return typeof payload.sub === 'string' ? payload.sub : null;
  } catch {
    return null;
  }
}

function decodeJwtVmInstanceId(token: string): string | null {
  try {
    const parts = token.split('.');
    if (parts.length < 2) return null;
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = Buffer.from(b64, 'base64').toString('utf8');
    const payload = JSON.parse(json) as { vm_instance_id?: unknown };
    return typeof payload.vm_instance_id === 'string' ? payload.vm_instance_id : null;
  } catch {
    return null;
  }
}

function parseParkDirective(result: unknown): ParkDirective | null {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return null;
  const body = result as Record<string, unknown>;
  if (body.action !== 'park') return null;
  const retryAfterUntil = typeof body.retry_after_until === 'string' ? body.retry_after_until : '';
  const parkDeadline = typeof body.park_deadline === 'string' ? body.park_deadline : '';
  if (!retryAfterUntil || !parkDeadline) return null;
  return { action: 'park', retryAfterUntil, parkDeadline };
}

const pendingParkDirectivesByTask = new Map<string, ParkDirective>();

function consumePendingParkDirective(taskId: string): ParkDirective | null {
  const directive = pendingParkDirectivesByTask.get(taskId) ?? null;
  pendingParkDirectivesByTask.delete(taskId);
  return directive;
}

export function _consumePendingParkDirectiveForTesting(taskId: string): ParkDirective | null {
  return consumePendingParkDirective(taskId);
}

function createPersistedStaleResultEnvelope(
  payload: Record<string, unknown>,
  agentToken = AGENT_TOKEN,
): PersistedStaleResultEnvelope {
  const envelope = createStaleResultEnvelope(payload);
  return agentToken ? { ...envelope, agentToken } : envelope;
}

function parsePersistedStaleResultFile(raw: string, now = Date.now()): PersistedStaleResultEnvelope {
  const envelope = parseStaleResultFile(raw, now);
  const parsed = JSON.parse(raw) as Record<string, unknown>;
  const agentToken = strVal(parsed.agentToken) || strVal(parsed.agent_token);
  return agentToken ? { ...envelope, agentToken } : envelope;
}

function markPersistedStaleResultFailure(
  envelope: PersistedStaleResultEnvelope,
  error: unknown,
  now = Date.now(),
): PersistedStaleResultEnvelope {
  const updated = markStaleResultFailure(envelope, error, now);
  return envelope.agentToken ? { ...updated, agentToken: envelope.agentToken } : updated;
}

function resolveReplayAgentToken(
  envelope: PersistedStaleResultEnvelope,
  staleTaskId: string,
): { agentToken: string | null; reason?: string } {
  if (envelope.agentToken) {
    return { agentToken: envelope.agentToken };
  }
  const currentTokenTaskId = decodeJwtSub(AGENT_TOKEN);
  if (currentTokenTaskId && currentTokenTaskId === staleTaskId) {
    return { agentToken: AGENT_TOKEN };
  }
  if (currentTokenTaskId) {
    return {
      agentToken: null,
      reason: `legacy envelope has no persisted agent token and current token belongs to ${currentTokenTaskId}`,
    };
  }
  return {
    agentToken: null,
    reason: 'legacy envelope has no persisted agent token and current token is not task-scoped',
  };
}

async function apiPostWithFixedToken(
  path: string,
  body: Record<string, unknown> = {},
  agentToken: string,
  retries = API_MAX_RETRIES,
  timeoutMs = API_TIMEOUT,
): Promise<unknown> {
  if (body.logs && typeof body.logs === 'string' && body.logs.length > LOGS_MAX_LENGTH) {
    body = { ...body, logs: body.logs.slice(0, LOGS_MAX_LENGTH) + '\n...(truncated)' };
  }
  const headers = {
    'Content-Type': 'application/json',
    'X-Agent-Token': agentToken,
  };
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => { controller.abort(); }, timeoutMs);
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (res.ok) return await res.json() as unknown;
      const text = await res.text();
      if (res.status === 401 && text.includes('no longer active') && path === '/api/agent/status') {
        log(`POST ${path} got 401 (task terminated) — previous POST likely succeeded`);
        return { status: 'already_terminal', task_id: body.task_id };
      }
      if (res.status === 429) {
        if (attempt === retries - 1) throw new Error(`POST ${path} failed (${res.status}): ${text}`);
        const retryAfterSec = parseInt(res.headers.get('Retry-After') || '5', 10);
        const delayMs = Math.max(1000, Math.min(retryAfterSec * 1000, API_RETRY_BACKOFF_MAX_MS));
        log(`POST ${path} got 429 (rate-limited/locked), retrying in ${delayMs}ms`);
        await new Promise(r => setTimeout(r, delayMs));
        continue;
      }
      if (res.status < 500) {
        throw new Error(`POST ${path} failed (${res.status}): ${text}`);
      }
      if (attempt === retries - 1) {
        throw new Error(`POST ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      if (attempt === retries - 1 || (e instanceof Error && /failed \(4\d{2}\)/.test(e.message))) throw e;
    }
    await new Promise(r => setTimeout(r, Math.min(1000 * Math.pow(2, attempt) + Math.random() * 1000, API_RETRY_BACKOFF_MAX_MS)));
  }
  throw new Error(`POST ${path} failed after ${retries} retries`);
}

function writeStaleResultFile(path: string, envelope: PersistedStaleResultEnvelope): void {
  writePrivateFileSync(path, JSON.stringify(envelope));
}

const pendingStatusFlagsByTask = new Map<string, Record<string, unknown>>();

function setPendingStatusFlag(taskId: string, key: string, value: unknown): void {
  const current = pendingStatusFlagsByTask.get(taskId) ?? {};
  pendingStatusFlagsByTask.set(taskId, { ...current, [key]: value });
}

function clearPendingStatusFlags(taskId: string): void {
  pendingStatusFlagsByTask.delete(taskId);
}

function statusFlagsForTask(taskId: string): Record<string, unknown> {
  return pendingStatusFlagsByTask.get(taskId) ?? {};
}

/**
 * True when an error is the backend's benign "task went terminal" 401 — i.e.
 * the task was CANCELLED/COMPLETED/superseded between /api/agent/claim and a
 * subsequent task-scoped GET (notably the startup /api/agent/github/token
 * fetch, which is intentionally NOT on verify_agent_token's idempotent-path
 * allowlist). The thrown apiGet error carries the shape
 * "GET /api/agent/... failed (401): {...\"Task is no longer active\"...}".
 *
 * This is NOT a code fault — it must not be recorded as failure_category
 * 'fatal'. Mirrors the graceful handling already in apiPost's 401 interceptor
 * (~line 362) and the heartbeat 4xx self-terminate branch (~line 1012).
 */
export function isTaskNoLongerActiveError(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  return /failed \(401\)/.test(e.message) && /no longer active/i.test(e.message);
}

function isTerminalStatus(status: unknown): boolean {
  return status === 'COMPLETED' || status === 'FAILED' || status === 'CANCELLED';
}

async function applyFinalStatusPrerequisites(
  taskId: string,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!isTerminalStatus(payload.status)) return payload;
  try {
    await rotateOAuthCredentialsIfChanged(taskId);
    return payload;
  } catch (e) {
    const message = sanitize(e instanceof Error ? e.message : String(e)).slice(0, 500);
    log(`[ERROR] OAuth rotation failed; failing task with oauth_rotation_failed so the backend does not reuse a revoked token: ${message}`);
    const existingLogs = typeof payload.logs === 'string' && payload.logs.trim()
      ? `${payload.logs}\n\n`
      : '';
    return {
      ...payload,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      logs: `${existingLogs}OAuth credential rotation failed after task execution: ${message}`,
      failure_category: 'oauth_rotation_failed',
      oauth_rotation_failed: true,
      oauth_rotation_error: message,
    };
  }
}

export async function persistAndPostStatus(taskId: string, payload: Record<string, unknown>) {
  const resultPath = `/tmp/task_${taskId}_result.json`;
  const statusPayload = await applyFinalStatusPrerequisites(taskId, payload);
  const enrichedPayload = enrichStatusPayload(taskId, statusPayload);
  writeStaleResultFile(resultPath, createPersistedStaleResultEnvelope(enrichedPayload));
  const result = await apiPost('/api/agent/status', enrichedPayload);
  const parkDirective = parseParkDirective(result);
  if (parkDirective) pendingParkDirectivesByTask.set(taskId, parkDirective);
  try { fs.unlinkSync(resultPath); } catch { /* ignore — file already gone */ }
  clearPendingStatusFlags(taskId);
  return result;
}

// Phase 0 telemetry — start-of-role timestamp captured by main() after a
// successful claim and reset at the start of each warm role. Used by
// enrichStatusPayload to derive setup_secs from firstToolUseAt.
let roleClaimTimestamp: number | null = null; // @per-role

// Phase 0 telemetry — claim → handler-complete elapsed seconds. Captured at
// the success path of runRoleOnce() right after the handler returns; rides on
// the next status payload via enrichStatusPayload. Reset to null between
// roles in the role-loop telemetry-reset block. Stays null on failure paths
// so the backend doesn't conflate fatal-error duration with completion time.
let handlerDurationSecsForPayload: number | null = null; // @per-role

/** Test-only: seed the role-claim timestamp so a test can simulate
 *  timing without going through main(). */
export function _setRoleClaimTimestampForTesting(value: number | null): void {
  roleClaimTimestamp = value;
}

/** Test-only: read the role-claim timestamp. */
export function _getRoleClaimTimestampForTesting(): number | null {
  return roleClaimTimestamp;
}

/** Test-only: seed the handler-duration value so a test can simulate the
 *  post-handler capture without invoking the full role-loop. */
export function _setHandlerDurationForTesting(value: number | null): void {
  handlerDurationSecsForPayload = value;
}

/** Test-only: read the handler-duration value. */
export function _getHandlerDurationForTesting(): number | null {
  return handlerDurationSecsForPayload;
}

function computeSetupSecs(): number | null {
  if (roleClaimTimestamp === null || firstToolUseAt === null) return null;
  const diff = (firstToolUseAt - roleClaimTimestamp) / 1000;
  // Defensive: never report negative setup_secs (clock skew, mis-ordered timestamps).
  return diff >= 0 ? diff : null;
}

function enrichStatusPayload(taskId: string, payload: Record<string, unknown>): Record<string, unknown> {
  // Phase 0 telemetry: setup_secs (claim → first tool use), warm_start, and
  // prior_role_vm_id ride along on every status payload. The backend's
  // task_lifecycle.py persists them under task.context["_timing"] when
  // present and ignores them otherwise. Compute eagerly so the shape is
  // stable across cold/warm/failed roles.
  const telemetry: Record<string, unknown> = {};
  const setupSecs = computeSetupSecs();
  if (setupSecs !== null) telemetry.setup_secs = setupSecs;
  // handler_duration_secs is captured by runRoleOnce after the handler returns
  // successfully; null on failure paths so a fatal-error duration doesn't get
  // logged as completion time.
  if (handlerDurationSecsForPayload !== null) {
    telemetry.handler_duration_secs = handlerDurationSecsForPayload;
  }
  // warm_start is a meaningful boolean even when false (cold roles); always
  // emit so the backend can distinguish "missing" from "explicitly false".
  telemetry.warm_start = isWarmStart;
  // prior_role_vm_id is the VM that ran the prior role. On a warm VM that's
  // ourselves (the same VM ran the prior role and is now running this one).
  // On cold roles there is no prior, so we send null.
  telemetry.prior_role_vm_id = isWarmStart ? (process.env.VM_INSTANCE_ID || null) : null;
  // Reviewer findings telemetry: when a fresh-reviewer pass ran and wrote
  // reviewer_findings into the self-review JSON, surface counts so the backend
  // can store them in task.context for catch-rate measurement (e.g. whether
  // reviewer-flagged categories were later confirmed by the human reviewer).
  // Best-effort: missing file or parse errors are silently ignored.
  if (isTerminalStatus(payload.status)) {
    const srPath = `/tmp/self_review_${taskId}.json`;
    try {
      const srRaw = fs.readFileSync(srPath, 'utf-8');
      const srData = JSON.parse(srRaw) as Record<string, unknown>;
      if (Array.isArray(srData.reviewer_findings) && srData.reviewer_findings.length > 0) {
        const findings = srData.reviewer_findings as Array<Record<string, unknown>>;
        telemetry.reviewer_findings_count = findings.length;
        telemetry.reviewer_critical_count = findings.filter(
          f => typeof f.severity === 'string' && f.severity === 'CRITICAL'
        ).length;
      }
    } catch { /* self-review file absent or unparseable — omit telemetry fields */ }
  }
  // Final flush of any buffered fix-loop events before the terminal status
  // post lands — heartbeats are best-effort, so this is the safety net that
  // guarantees the per-task fix_loop_history is at least eventually accurate.
  // Merge with any events the caller already passed in (rare, but explicit).
  const flushedEvents = collectFixLoopEventsForFlush();
  let mergedFixLoopEvents: unknown = (payload as { fix_loop_events?: unknown }).fix_loop_events;
  if (flushedEvents.length > 0) {
    const merged = Array.isArray(mergedFixLoopEvents)
      ? [...(mergedFixLoopEvents as unknown[]), ...flushedEvents]
      : flushedEvents;
    // Backend caps the field at 20 per request — drop oldest first so the
    // most recent terminal events (e.g. exit_reason='passed') always land.
    mergedFixLoopEvents = merged.length > 20 ? merged.slice(-20) : merged;
  }
  // Ship the structured provider-run error report with every FAILED status so
  // backend classification consumes the truthful exit context (exit code,
  // stderr tail, provider error events, run shape) instead of guessing from
  // output-text tails. The runner records a report for EVERY run (success or
  // failure), so a multi-pass handler's terminal post always carries the LAST
  // run's report. The task_id guard prevents a warm VM from leaking the prior
  // role's report into a failure that never ran the provider CLI (e.g.
  // clone_failed before any model run).
  let errorReport: ProviderRunErrorReport | null = null;
  if (payload.status === 'FAILED') {
    const report = getLastProviderRunErrorReport();
    if (report && report.task_id === taskId) errorReport = report;
  }
  const workspacePrepOutcome = getLastWorkspacePrepOutcome();
  return {
    provider: PROVIDER,
    auth_mode: AUTH_MODE,
    model: process.env.LLM_MODEL || undefined,
    execution_mode: LLM_EXECUTION_MODE,
    task_id: taskId,
    ...telemetry,
    ...(errorReport ? { error_report: errorReport } : {}),
    ...payload,
    ...statusFlagsForTask(taskId),
    ...(workspacePrepOutcome !== null ? { workspace_prep: workspacePrepOutcome } : {}),
    ...(mergedFixLoopEvents !== undefined ? { fix_loop_events: mergedFixLoopEvents } : {}),
  };
}

export async function apiGet(path: string, retries = API_MAX_RETRIES, timeoutMs = API_TIMEOUT) {
  return apiRequest('GET', path, () => ({ headers: apiHeaders }), retries, timeoutMs);
}

// ── Task-capability contract (agent permission source of truth) ──────────────
// The agent derives its read_only/write task partition from the published
// backend contract (the same registry operator surfaces consume) rather than a
// hand-maintained agent table. Fetch the contract from the agent-authed
// endpoint and load it into taskCapabilities once per VM, BEFORE any handler
// dispatch — so configureAgentPermissions/isReadOnlyTaskType resolve against
// real backend metadata. A missing or incomplete contract throws, failing the
// role loudly instead of silently misclassifying permissions.

let _taskCapabilityContractLoaded = false;

export async function fetchTaskCapabilityContract(): Promise<TaskCapabilityContract> {
  // Use the same 180s timeout as the github/token fetch — both endpoints call
  // get_github_token_for_task under the hood, which can legitimately block up to
  // ~120s while _github_request sleeps on a near-empty rate-limit bucket.
  const res = await apiGet('/api/agent/task-capabilities', API_MAX_RETRIES, 180_000) as { task_capabilities?: TaskCapabilityContract };
  const contract = res.task_capabilities;
  if (!contract || typeof contract !== 'object') {
    throw new Error('GET /api/agent/task-capabilities returned no task_capabilities map');
  }
  return contract;
}

// Idempotent: fetches + loads the contract on first call per VM, then no-ops.
// The optional fetcher injection point exists for tests; production callers
// omit it and use the real endpoint fetch.
export async function ensureTaskCapabilityContractLoaded(
  fetcher: () => Promise<TaskCapabilityContract> = fetchTaskCapabilityContract,
): Promise<void> {
  if (_taskCapabilityContractLoaded) return;
  const contract = await fetcher();
  loadTaskCapabilityContract(contract);
  _taskCapabilityContractLoaded = true;
}

// Test-only: reset the once-per-VM guard so the load flow can be re-exercised.
export function __resetTaskCapabilityContractLoadedForTesting(): void {
  _taskCapabilityContractLoaded = false;
}

export function log(msg: string) {
  console.log(`[${new Date().toISOString()}] ${sanitize(msg)}`);
}

// ── Warm-VM continuity polling (Phase 2a) ───────────────────────────────────
// pollForNextRole is the agent-side counterpart to the backend's
// /api/agent/next-role + /api/agent/vm-heartbeat endpoints. After a role
// finishes successfully, the agent calls this function instead of exiting.
// It polls the orchestrator for a warm-pinned QUEUED task targeting our VM,
// firing vm-heartbeat keepalives in parallel so the reaper doesn't kill us
// during the poll window. Returns either a claim (next task to run) or an
// exit (no more roles in this chain).
//
// The agent reads neither warm_continuity_enabled nor next_role_poll_timeout_secs
// — the backend gates the feature; the agent just calls the endpoints and
// honours whatever response shape comes back. The only knob agents see is
// the poll timeout default (180s), which matches the backend default.

// Provider secret manifest shape, identical to what build_agent_secret_manifest
// (backend/app/compute_manifest.py) produces and what the boot-time
// materialize_manifest.py consumes: an env dict (provider/auth/model + secret
// env), provider credential files, and metadata. The backend includes this
// under the next-role response's `secret_manifest` when the warm-claimed task
// changes LLM identity or needs a newer same-account rotating credential.
export interface SecretManifest {
  env?: Record<string, unknown>;
  files?: { path: string; content?: string; mode?: string }[];
  metadata?: Record<string, unknown>;
}

export interface PollResult {
  action: 'claim' | 'exit';
  taskId?: string;
  reason?: string;
  // On a warm-start claim the backend mints a fresh JWT scoped to the new
  // task. The agent must rotate AGENT_TOKEN to this value before any
  // subsequent task-scoped call (status updates, github proxy, claim
  // idempotent-replay), since the prior task's JWT is terminal and only
  // authenticates the idempotent next-role/vm-heartbeat paths.
  agentToken?: string;
  // Present only on an account-divergent warm claim: the resolved account's
  // provider secret manifest. When set, the role loop must re-materialize
  // credentials and re-exec so the frozen provider constants are re-read on
  // the correct account. Absent (same-account claim) → JWT-only rotation.
  secretManifest?: SecretManifest;
  // Lane workspace-mode fields. Present only when the claimed task is a lane
  // phase and improvement_lanes_enabled is on. The agent uses laneCheckpointSha
  // to hard-reset the warm workspace to the last verified checkpoint SHA
  // (treating it as truth) rather than resetting to origin/<branch>.
  laneCheckpointSha?: string;
  isLanePhase?: boolean;
}

export interface ParkDirective {
  action: 'park';
  retryAfterUntil: string;
  parkDeadline: string;
}

export interface PollForNextRoleOptions {
  vmInstanceId: string;
  priorTaskId: string;
  priorPrNumber: number | null;
  apiBase: string;
  agentJwt: string;
  pollIntervalMs?: number;
  timeoutMs?: number;
  heartbeatIntervalMs?: number;
}

export interface PoolBootPollOptions {
  vmInstanceId: string;
  apiBase: string;
  agentJwt: string;
  maxIdleLifetimeMs?: number;
  pollWindowMs?: number;
  pollIntervalMs?: number;
  heartbeatIntervalMs?: number;
  pollOnce?: (opts: PollForNextRoleOptions) => Promise<PollResult>;
}

export async function pollForNextRole(opts: PollForNextRoleOptions): Promise<PollResult> {
  const {
    vmInstanceId,
    priorTaskId,
    priorPrNumber,
    apiBase,
    agentJwt,
    pollIntervalMs = 5000,
    timeoutMs = 180_000,
    heartbeatIntervalMs = 30_000,
  } = opts;

  const start = Date.now();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Agent-Token': agentJwt,
  };

  // Shared revoke flag — set by either heartbeat path or next-role path on
  // an explicit 401/403 from the backend. Polling without a valid JWT just
  // burns the timeout, so we exit fast and let the orchestrator cold-
  // provision a fresh VM with a freshly minted token.
  // Wrapper object: type-narrowing in TypeScript can't see through async
  // closures that mutate a let-bound boolean, so the unnecessary-condition
  // rule fires false-positively. A field-on-object dodges that limitation.
  const authState: { revoked: boolean } = { revoked: false };

  // VM-heartbeat keepalive — runs in parallel with the poll loop so the
  // reaper sees this VM as alive even though the task heartbeat stopped
  // when the prior role completed. setInterval, NOT setTimeout-recursion,
  // so a slow heartbeat call doesn't accumulate latency into the poll.
  const heartbeatTimer = setInterval(() => {
    void (async () => {
      try {
        const res = await fetch(`${apiBase}/api/agent/vm-heartbeat`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ vm_instance_id: vmInstanceId, vm_image_name: vmImageName, vm_image_build_ts: vmImageTs, agent_runtime_fingerprint: agentRuntimeFingerprint, ...(agentSourceSha ? { agent_source_sha: agentSourceSha } : {}) }),
        });
        if (res.status === 401 || res.status === 403) {
          log(`pollForNextRole vm-heartbeat got ${String(res.status)} — auth revoked, will exit poll`);
          authState.revoked = true;
        }
      } catch (e) {
        log(`pollForNextRole vm-heartbeat error: ${String(e)}`);
      }
    })();
  }, heartbeatIntervalMs);

  // Single fire-and-forget initial heartbeat so we don't wait
  // heartbeatIntervalMs for the first liveness signal — the next-role poll
  // itself records liveness backend-side, but explicit heartbeat is the
  // documented contract.
  void (async () => {
    try {
      const res = await fetch(`${apiBase}/api/agent/vm-heartbeat`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ vm_instance_id: vmInstanceId, vm_image_name: vmImageName, vm_image_build_ts: vmImageTs, agent_runtime_fingerprint: agentRuntimeFingerprint, ...(agentSourceSha ? { agent_source_sha: agentSourceSha } : {}) }),
      });
      if (res.status === 401 || res.status === 403) {
        log(`pollForNextRole initial vm-heartbeat got ${String(res.status)} — auth revoked, will exit poll`);
        authState.revoked = true;
      }
    } catch (e) {
      log(`pollForNextRole initial vm-heartbeat error: ${String(e)}`);
    }
  })();

  try {
    while (Date.now() - start < timeoutMs) {
      if (authState.revoked) {
        return { action: 'exit', reason: 'auth_revoked' };
      }
      try {
        const res = await fetch(`${apiBase}/api/agent/next-role`, {
          method: 'POST',
          headers,
          body: JSON.stringify({
            vm_instance_id: vmInstanceId,
            prior_task_id: priorTaskId,
            prior_pr_number: priorPrNumber,
          }),
        });
        // 401/403 — JWT revoked or otherwise rejected. Polling further is
        // hopeless; exit so the orchestrator can cold-provision with fresh
        // creds. We treat this as terminal even on the first iteration.
        if (res.status === 401 || res.status === 403) {
          log(`pollForNextRole next-role got ${String(res.status)} — auth revoked, exiting`);
          return { action: 'exit', reason: 'auth_revoked' };
        }
        // 204 No Content — keep polling.
        if (res.status === 204) {
          await new Promise(r => setTimeout(r, pollIntervalMs));
          continue;
        }
        // 200 OK with a JSON body that selects claim or exit.
        if (res.status === 200) {
          let body: { action?: string; task_id?: string; reason?: string; agent_token?: string; secret_manifest?: unknown; lane_checkpoint_sha?: unknown; is_lane_phase?: unknown } = {};
          try {
            body = await res.json() as { action?: string; task_id?: string; reason?: string; agent_token?: string; secret_manifest?: unknown; lane_checkpoint_sha?: unknown; is_lane_phase?: unknown };
          } catch (e) {
            log(`pollForNextRole: 200 with unparseable body: ${String(e)}`);
            await new Promise(r => setTimeout(r, pollIntervalMs));
            continue;
          }
          if (body.action === 'claim' && typeof body.task_id === 'string') {
            // A manifest re-materializes credentials for either an account
            // change or a same-account rotating-credential refresh. The latter
            // is marked in metadata and does not require a process re-exec.
            const sm = body.secret_manifest;
            const secretManifest = (sm && typeof sm === 'object' && !Array.isArray(sm))
              ? (sm as SecretManifest)
              : undefined;
            return {
              action: 'claim',
              taskId: body.task_id,
              agentToken: typeof body.agent_token === 'string' ? body.agent_token : undefined,
              ...(secretManifest ? { secretManifest } : {}),
              ...(typeof body.lane_checkpoint_sha === 'string' ? { laneCheckpointSha: body.lane_checkpoint_sha } : {}),
              ...(body.is_lane_phase === true ? { isLanePhase: true } : {}),
            };
          }
          if (body.action === 'exit') {
            return { action: 'exit', reason: body.reason };
          }
          log(`pollForNextRole: 200 with unrecognised action=${String(body.action)}`);
          await new Promise(r => setTimeout(r, pollIntervalMs));
          continue;
        }
        // Other non-2xx — treat as transient, log and keep polling.
        log(`pollForNextRole: HTTP ${String(res.status)} — continuing to poll`);
        await new Promise(r => setTimeout(r, pollIntervalMs));
      } catch (e) {
        // Network error, JSON error etc. Don't crash the agent — keep polling.
        log(`pollForNextRole fetch error: ${String(e)}`);
        await new Promise(r => setTimeout(r, pollIntervalMs));
      }
    }
    if (authState.revoked) {
      return { action: 'exit', reason: 'auth_revoked' };
    }
    return { action: 'exit', reason: 'poll_timeout' };
  } finally {
    clearInterval(heartbeatTimer);
  }
}

// ── Warm-VM provider re-targeting (account-divergent warm claim) ──────────────
//
// A warm VM runs many sequential roles in ONE process. The provider/auth/
// execution-mode constants (PROVIDER, AUTH_MODE, LLM_EXECUTION_MODE,
// LLM_CLI_TRANSLATION in providers/runners.ts) are FROZEN at import from the
// boot /etc/agent.env, and the provider secret material (CLAUDE_CODE_OAUTH_TOKEN,
// ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ZAI_API_KEY, DEEPSEEK_API_KEY, GEMINI_API_KEY,
// CODEX_HOME, ...) lives in the process env +
// on-disk credential files written at boot. So when the backend warm-claims a
// task that resolves to a DIFFERENT LLM account, a JWT-only rotation cannot
// adopt the new material — the role would run on the wrong provider.
//
// performWarmRetarget re-materializes the new account's credentials and then
// performs a controlled re-exec (systemd unit restart) so the constants are
// re-read from the freshly written /etc/agent.env. This reuses the boot path
// rather than refactoring the ~50 frozen-const call sites into per-role getters.

// Absolute path to the materialize helper baked into the agent VM image
// alongside the agent source (infra/packer copies agent/src → /opt/agent/src).
// It is a byte-identical mirror of backend/app/_agent_scripts/materialize_manifest.py
// (CI enforces parity); the boot path embeds the same canonical file. Reusing
// the real helper avoids a parallel materializer + the cross-language escaping
// hazards of inlining the script (see CLAUDE.md).
export const MATERIALIZE_SCRIPT_PATH = fileURLToPath(
  new URL('./_agent_scripts/materialize_manifest.py', import.meta.url),
);

// Injectable side effects of the re-target path, so unit tests can assert the
// materialize → verify → clear-stale → re-exec sequence without touching sudo /
// systemd / the real filesystem. Mirrors the __setRunClaudeCodeOverrideForSmoke
// pattern.
export interface WarmRetargetDeps {
  /** Run the real materialize helper as root to (re)write envPath + cred files. */
  materialize: (manifest: SecretManifest, envPath: string) => void;
  /** Post-write check: returns the manifest-owned paths missing from disk. */
  verifyMaterialized: (manifest: SecretManifest, envPath: string) => string[];
  /** Remove prior-account credential paths the new manifest does NOT own. */
  clearStaleCreds: (manifest: SecretManifest) => Promise<void>;
  /** Re-exec the agent process so frozen provider constants are re-read. */
  reexec: () => Promise<never> | Promise<void>;
}

function defaultMaterialize(manifest: SecretManifest, envPath: string): void {
  // The helper writes /etc/agent.env (root-owned 0600) and chowns provider
  // credential files to the agent user, so it must run as root — exactly as the
  // boot path invokes it. The manifest JSON itself carries no extra escaping
  // burden (it is plain JSON.stringify), and the embedded script is a real
  // file, so the cross-language code-generation hazards do not apply here.
  const manifestPath = `/tmp/agent_retarget_manifest_${process.pid}.json`;
  writePrivateFileSync(manifestPath, JSON.stringify(manifest));
  try {
    execFileSync('sudo', ['python3', MATERIALIZE_SCRIPT_PATH, manifestPath, envPath], {
      timeout: 60_000,
      stdio: 'pipe',
    });
  } finally {
    try { fs.unlinkSync(manifestPath); } catch { /* ignore — best effort */ }
  }
}

function defaultVerifyMaterialized(manifest: SecretManifest, envPath: string): string[] {
  // existsSync (not access checks): the env file is root-owned 0600 and the
  // agent user can stat-but-not-read it; presence is the contract we verify.
  const missing: string[] = [];
  const expected = [envPath, ...(manifest.files ?? []).map(f => f.path)];
  for (const p of expected) {
    try {
      if (!fs.existsSync(p)) missing.push(p);
    } catch {
      // A failed stat must not abort an otherwise-successful re-target.
    }
  }
  return missing;
}

function defaultClearStaleCreds(manifest: SecretManifest): Promise<void> {
  // Preserve the SAME resolved locations materializeCredentialState wrote, so
  // the stale-clear never deletes the file the new account just materialized.
  // No-op remap on the cloud VM (HOME=/home/agent → identical paths).
  return clearStaleProviderCredentials(
    (manifest.files ?? []).map(f => resolveManagedCredentialWritePath(f.path)),
  );
}

async function defaultReexec(): Promise<never> {
  // Suppress this (about-to-be-killed) process's SIGTERM crash report: the
  // restart SIGTERMs us and the FRESH process owns the just-claimed task. With
  // TASK_ID blanked, _crashReport's `if (taskId)` gate skips, so the benign
  // restart never overwrites the new task's status with a FATAL message.
  requestCancel();
  process.env.TASK_ID = '';
  // --no-block returns immediately instead of blocking until the unit (us!)
  // stops; systemd then asynchronously SIGTERMs this process and starts a fresh
  // one that re-reads /etc/agent.env. A blocking restart would deadlock: the
  // synchronous execFileSync would hold the event loop so our SIGTERM handler
  // could never run, hanging until TimeoutStopSec → SIGKILL.
  execFileSync('sudo', ['systemctl', 'restart', '--no-block', 'blueprint-agent'], {
    timeout: 30_000,
    stdio: 'pipe',
  });
  log('Warm re-target: systemd restart requested — awaiting SIGTERM to re-exec on new provider');
  // Block forever; the incoming SIGTERM tears us down. Never resolves so the
  // role loop cannot re-enter and run on stale credentials.
  return new Promise<never>(() => { /* resolved only by process teardown */ });
}

function defaultWarmRetargetDeps(): WarmRetargetDeps {
  return {
    materialize: defaultMaterialize,
    verifyMaterialized: defaultVerifyMaterialized,
    clearStaleCreds: defaultClearStaleCreds,
    reexec: defaultReexec,
  };
}

let warmRetargetDeps: WarmRetargetDeps = defaultWarmRetargetDeps();

/** Test-only: override the warm-retarget side effects. */
export function __setWarmRetargetDepsForTesting(deps: Partial<WarmRetargetDeps> | null): void {
  warmRetargetDeps = { ...defaultWarmRetargetDeps(), ...(deps ?? {}) };
}

/**
 * Re-target a warm VM onto the account-divergent claim's provider credentials.
 *
 * 1. Re-inject the rotated JWT + task id + warm-handoff/re-exec markers the
 *    backend dropped/omitted from the manifest, then materialize the new
 *    account's manifest (rewrites /etc/agent.env + provider cred files IN
 *    PLACE) and verify the files landed on disk.
 * 2. Clear the prior account's stale credential paths — only the ones the new
 *    manifest does NOT own (same-path creds were already overwritten in step 1).
 * 3. Controlled re-exec so the frozen provider constants are re-read and the
 *    role resumes on the correct provider.
 *
 * Write-new-then-swap ordering (2026-06-06 poisoned-VM incidents): the old
 * clear-then-write sequence had a configless window — if the process died after
 * the clear, the VM was left with no /etc/agent.env and no ~/.claude.json,
 * looked healthy, and every task it claimed died in seconds. Now there is no
 * instant without a complete config: until materialize finishes, the prior
 * account's config is intact; afterwards the new account's is. The
 * no-cross-account-leakage property is preserved because the role only ever
 * starts AFTER step 2 (a role runs only post-reexec), and a same-provider
 * re-target's credential files share paths, so step 1's overwrite already
 * destroyed the prior account's material.
 *
 * In production step 3 never returns (the restart SIGTERMs us). The injectable
 * deps make the sequence assertable in tests.
 */
export async function performWarmRetarget(
  secretManifest: SecretManifest,
  opts: { rotatedJwt: string; taskId: string },
  deps: WarmRetargetDeps = warmRetargetDeps,
): Promise<void> {
  // 1. The backend strips AGENT_SECRET_TOKEN from the manifest (the authoritative
  //    rotated JWT rides the response's agent_token), so re-inject it here. TASK_ID
  //    pins the just-claimed role so the re-exec'd process resumes THAT claim
  //    (idempotent /api/agent/claim replay) rather than re-polling. The warm
  //    markers tell main() to keep the workspace warm and count this as a warm role.
  const env: Record<string, unknown> = { ...(secretManifest.env ?? {}) };
  env.AGENT_SECRET_TOKEN = opts.rotatedJwt;
  if (opts.taskId) env.TASK_ID = opts.taskId;
  env.BLUEPRINT_WARM_HANDOFF = '1';
  env.BLUEPRINT_REEXEC = '1';
  // Carry lane workspace-mode vars into the re-exec'd /etc/agent.env so the
  // restarted process sees the same lane state as this one. Set by
  // continueWarmContinuation before performWarmRetarget is called.
  if (process.env.BLUEPRINT_LANE_MODE === '1') {
    env.BLUEPRINT_LANE_MODE = '1';
    if (process.env.BLUEPRINT_LANE_CHECKPOINT_SHA) {
      env.BLUEPRINT_LANE_CHECKPOINT_SHA = process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;
    }
  }
  const manifest: SecretManifest = { ...secretManifest, env };

  // Materialize FIRST (write-new-then-swap): the helper overwrites
  // /etc/agent.env and every new-account credential file in place, so a death
  // anywhere in this sequence leaves the VM with a complete config — either
  // still the prior account's (recoverable: the throw below surfaces
  // reset_failed and the orchestrator cold-provisions) or already the new one.
  deps.materialize(manifest, '/etc/agent.env');
  const missing = deps.verifyMaterialized(manifest, '/etc/agent.env');
  if (missing.length > 0) {
    throw new Error(
      `warm re-target verification failed — materialized config missing on disk: ${missing.join(', ')}`,
    );
  }

  // 2. Now (and only now) remove the prior account's stale credential paths the
  //    new manifest does not own. Best-effort like the pre-existing clear: the
  //    role cannot start until reexec, so a logged-and-swallowed rm failure here
  //    is the same residual exposure the old ordering had.
  await deps.clearStaleCreds(manifest);

  // 3. Re-exec onto the freshly written /etc/agent.env.
  await deps.reexec();
}

const DEFAULT_POOL_MAX_IDLE_LIFETIME_MS = 60 * 60 * 1000;
const INITIAL_POOL_PRIOR_TASK_ID = '00000000-0000-0000-0000-000000000000';

function parseDurationEnvMs(name: string, fallbackMs: number): number {
  const raw = process.env[name];
  if (!raw) return fallbackMs;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    log(`Ignoring invalid ${name}=${raw}; using ${fallbackMs}ms`);
    return fallbackMs;
  }
  return Math.floor(parsed);
}

function getPoolMaxIdleLifetimeMs(): number {
  return parseDurationEnvMs('BLUEPRINT_POOL_MAX_IDLE_LIFETIME_MS', DEFAULT_POOL_MAX_IDLE_LIFETIME_MS);
}

function activateClaimedRole(
  claim: Pick<PollResult, 'taskId' | 'agentToken'>,
  missingTokenErrorPrefix: string,
): void {
  if (claim.agentToken) {
    AGENT_TOKEN = claim.agentToken;
    apiHeaders['X-Agent-Token'] = AGENT_TOKEN;
    // Mirror the freshly-claimed token to the live token file so a resident
    // session provisioned for this role starts with a current token on disk.
    writeAgentTokenFile(AGENT_TOKEN);
    stopProactiveTokenRefresh();
    scheduleProactiveTokenRefresh(AGENT_TOKEN);
  } else {
    const err = new Error(`${missingTokenErrorPrefix}: missing agent_token in next-role response`) as Error & {
      failureCategory?: FailureCategory;
    };
    err.failureCategory = 'reset_failed';
    throw err;
  }
  if (claim.taskId) {
    process.env.TASK_ID = claim.taskId;
    setCrashReportTaskId(claim.taskId);
  }
  _inWarmPollWindow = false;
}

export async function bootstrapPoolMode(opts: PoolBootPollOptions): Promise<PollResult> {
  const {
    vmInstanceId,
    apiBase,
    agentJwt,
    maxIdleLifetimeMs = getPoolMaxIdleLifetimeMs(),
    pollWindowMs = 180_000,
    pollIntervalMs,
    heartbeatIntervalMs,
    pollOnce = pollForNextRole,
  } = opts;
  const startedAt = Date.now();

  while (Date.now() - startedAt < maxIdleLifetimeMs) {
    const remainingMs = maxIdleLifetimeMs - (Date.now() - startedAt);
    const result = await pollOnce({
      vmInstanceId,
      priorTaskId: INITIAL_POOL_PRIOR_TASK_ID,
      priorPrNumber: null,
      apiBase,
      agentJwt,
      timeoutMs: Math.min(pollWindowMs, remainingMs),
      ...(pollIntervalMs !== undefined ? { pollIntervalMs } : {}),
      ...(heartbeatIntervalMs !== undefined ? { heartbeatIntervalMs } : {}),
    });
    if (result.action === 'claim') {
      if (bootCredentialSnapshot === null) {
        captureBootCredentialSnapshot();
      }
      activateClaimedRole(result, 'reset_failed: pool-mode first claim');
      materializeClaimCredentialState(result.secretManifest, 'reset_failed: pool-mode first claim');
      return result;
    }
    if (result.reason !== 'poll_timeout') return result;
  }
  return { action: 'exit', reason: 'max_idle_lifetime_exceeded' };
}

// ── Tool-use ring buffer ────────────────────────────────────────────────────
// Captures the most recent tool invocations so a post-mortem on AGENT_TIMEOUT
// reap can see what the agent was doing in the seconds before the heartbeat
// went silent. Sent with every heartbeat; the backend persists it as
// task.context._tool_trace, so the last-successful-heartbeat value IS the
// death-trace when the reaper later kills the task.
type ToolTraceEntry = { t: string; tool: string; input_preview: string };
const _toolTrace: ToolTraceEntry[] = [];
const TOOL_TRACE_MAX = 20;

// Phase 0 telemetry — first-tool-use timestamp per role. Captured by
// recordToolUse on its first call after a fresh role starts. Reset to null
// at the start of main() and at the start of each warm role so the next
// role gets its own setup_secs measurement. Module-level mutable so the
// SDK message loop (which records tool uses) doesn't need a handle to
// per-role state.
let firstToolUseAt: number | null = null; // @per-role

// Phase 0 telemetry — true when the current role was started via
// pollForNextRole on a warm VM, false on the initial cold-start role.
let isWarmStart = false; // @per-role

// True when the agent is running in Cloud Run Job mode. Cloud Run *automatically*
// sets CLOUD_RUN_JOB to the job name on every job execution (it is a reserved env
// name the backend cannot inject), so presence — not a specific value — is the
// signal. Set at startup; never set on the GCE VM path.
let isJobMode = false;

export function recordToolUse(toolName: string, input: unknown): void {
  if (firstToolUseAt === null) {
    firstToolUseAt = Date.now();
  }
  let preview: string;
  try {
    preview = typeof input === 'string' ? input : JSON.stringify(input);
  } catch {
    preview = '<unserializable>';
  }
  _toolTrace.push({
    t: new Date().toISOString(),
    tool: toolName,
    input_preview: sanitize(preview).slice(0, 200),
  });
  if (_toolTrace.length > TOOL_TRACE_MAX) _toolTrace.shift();
}

/** Test-only: read the first-tool-use timestamp captured this role. */
export function _getFirstToolUseAtForTesting(): number | null {
  return firstToolUseAt;
}

/** Test-only: reset per-role telemetry state between cases. */
export function _resetTelemetryForTesting(): void {
  firstToolUseAt = null;
  isWarmStart = false;
  setCurrentPromptFingerprint(null);
}

/** Test-only: set the warm-start flag (mirrors what the role loop does). */
export function _setWarmStartForTesting(value: boolean): void {
  isWarmStart = value;
}

/** Test-only: read the warm-start flag. */
export function _getWarmStartForTesting(): boolean {
  return isWarmStart;
}

/** Test-only: read the job-mode flag. */
export function _getJobModeForTesting(): boolean {
  return isJobMode;
}

/** Test-only: set the job-mode flag. */
export function _setJobModeForTesting(value: boolean): void {
  isJobMode = value;
}

/** Test-only: set the baked agent-runtime fingerprint used in heartbeats. */
export function _setAgentRuntimeFingerprintForTesting(value: string | null): void {
  agentRuntimeFingerprint = value;
}

/** Test-only: set the baked source git SHA used in heartbeats. */
export function _setAgentSourceShaForTesting(value: string | null): void {
  agentSourceSha = value;
}

export function snapshotToolTrace(options?: { refreshLastEntry?: boolean }): ToolTraceEntry[] {
  if (options?.refreshLastEntry && _toolTrace.length > 0) {
    _toolTrace[_toolTrace.length - 1] = {
      ..._toolTrace[_toolTrace.length - 1],
      t: new Date().toISOString(),
    };
  }
  return _toolTrace.slice();
}

// Drain accumulated fix-loop events for a heartbeat / status post. Also
// runs the Claude-driven `./check` synthesizer over the current tool-trace
// snapshot — it pushes any new events into the same buffer so a single
// drain returns wrapper-emitted + synthesized events together.
//
// Capped at the backend's per-request limit (20). When more accumulate
// between heartbeats, the buffer itself drops the oldest, so callers see
// at most MAX_BUFFERED_EVENTS here.
export function collectFixLoopEventsForFlush(): FixLoopEvent[] {
  try {
    const synthesized = synthesizeCheckFixLoopEvents(snapshotToolTrace());
    for (const ev of synthesized) pushFixLoopEvent(ev);
  } catch (e) {
    // Synthesizer failures must not break the heartbeat. Log and carry on.
    log(`fix-loop synthesizer failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  return drainFixLoopEventBuffer();
}

/**
 * Clear the tool-trace ring buffer.
 *
 * Called between warm roles by the role-loop reset block so the next role's
 * extractInvestigatedFiles() / heartbeat snapshot doesn't mix prior-role tool
 * uses with current-role ones. (Without this, a fix agent's tool trace would
 * carry over the reviewer's reads — biasing the next role's fingerprint.)
 */
export function clearToolTrace(): void {
  _toolTrace.length = 0;
}

// Extract repo-relative file paths the agent looked at during a task. Used to
// hand the reviewer's working set forward to the fix agent so it doesn't have
// to rediscover which files matter. Reads input_preview (truncated to 200
// chars by recordToolUse) with a regex so a JSON-truncated entry still yields
// the leading path. WORKSPACE prefix is stripped so the consumer sees
// repo-relative paths regardless of where the producing agent ran.
const _INVESTIGATION_TOOLS = new Set(['Read', 'Edit', 'Write', 'NotebookEdit', 'Grep', 'Glob']);
export function extractInvestigatedFiles(trace: ToolTraceEntry[]): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const entry of trace) {
    if (!_INVESTIGATION_TOOLS.has(entry.tool)) continue;
    const m = entry.input_preview.match(/"(?:file_path|path)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/);
    if (!m) continue;
    let p: string;
    try { p = JSON.parse(`"${m[1]}"`) as string; } catch { p = m[1]; }
    p = p.replace(/\\/g, '/').trim();
    if (p.startsWith(`${WORKSPACE}/`)) p = p.slice(WORKSPACE.length + 1);
    if (p.startsWith('/tmp/workspace/')) p = p.slice('/tmp/workspace/'.length);
    if (!p || p.includes('\n') || p === '.' || p === '/') continue;
    if (!seen.has(p)) { seen.add(p); ordered.push(p); }
  }
  return ordered;
}

// ── Per-role module-state isolation (warm-VM continuity) ─────────────────────
//
// On a warm VM a single agent process runs many sequential roles. EVERY piece
// of module-level mutable state that scopes to "the current role" must be reset
// here so a reused VM cannot leak state from the prior task into the next one.
// The warm-handoff path in main() calls this exactly once per transition,
// alongside resetForNextRole (which handles the filesystem/session side).
//
// Keep this the SINGLE source of truth for per-role state resets: when adding a
// new module-level `let` that holds per-task state, reset it here rather than
// re-adding an ad-hoc reset line at the call site. `flags` lets callers opt out
// of resets they will perform with different values (e.g. the role loop sets
// isWarmStart=true rather than the cold-start default false).
interface ResetPerRoleModuleStateFlags {
  /** Value to leave isWarmStart at (warm handoff sets true; tests use false). */
  warmStart?: boolean;
}
export function resetPerRoleModuleState(flags: ResetPerRoleModuleStateFlags = {}): void {
  // Shutdown signal — must start false so a leaked "cancel" never suppresses
  // the next role's heartbeats. See the cancelRequested declaration comment.
  resetCancelRequested();
  // Phase-0 telemetry: first-tool-use + warm-start marker.
  firstToolUseAt = null;
  isWarmStart = flags.warmStart ?? false;
  roleClaimTimestamp = null;
  handlerDurationSecsForPayload = null;
  // Tool-trace ring buffer — without clearing, the next role's
  // extractInvestigatedFiles() / heartbeat trace mixes in prior-role tool uses.
  clearToolTrace();
  // Prompt fingerprint attached to heartbeats.
  setCurrentPromptFingerprint(null);
  // `./check` synthesizer's monotonic iteration counter + started_at.
  resetCheckSynthesizerState();
  // Per-task LLM call/token telemetry (efficiency direction d-91d8dede).
  resetLlmTelemetry();
  // Per-task structured-output format-compliance telemetry (equipping d-46468a41).
  resetFormatComplianceTelemetry();
  // Structured provider-run error report. enrichStatusPayload already guards
  // attachment by task_id, but clear between roles anyway so a warm role can
  // never observe the prior role's run report through the getter.
  clearLastProviderRunErrorReport();
  // Workspace-prep outcome recorded by prepareWorkspaceForRole. Reset so a
  // stale outcome from the prior role never rides the next role's payload.
  resetWorkspacePrepOutcome();
  // Log ring buffer — without clearing, prior-role lines replay on the next
  // task's websocket connect and can trip the backend's 20 msg/s close-threshold.
  clearLogBuffer();
  // Heartbeat mutex — reset so a stale in-flight flag from the prior role
  // doesn't silence the new role's first periodic tick. stopHeartbeat() clears
  // these too, but resetting here makes resetPerRoleModuleState self-contained
  // regardless of call ordering.
  // (heartbeatGeneration is intentionally left monotonic — it only needs to be
  // unique per tick, never reset between roles.)
  heartbeatInFlight = false;
  heartbeatInFlightSince = 0;
  // Test convention cache in discovery.ts is per-repo; stale entries from a
  // prior task's codebase must not carry into a new role on a warm VM.
  clearTestConventionCache();
  // File-tree cache in discovery.ts — buildFileTree result is stale when the
  // workspace changes between roles.
  clearFileTreeCache();
  // Per-task resident-session turn-latency telemetry.
  resetResidentTurnLatencyTelemetry();
  // Disk-metric cache — clear so the first heartbeat of each task gets a fresh
  // df reading rather than reusing the prior task's cached values.
  _dfCacheTimestamp = 0;
}

// ── Heartbeat ───────────────────────────────────────────────────────────────
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

// ── Disk-metric cache ──────────────────────────────────────────────────────────
// collectMetrics calls 'df' every heartbeat (2+ times/min) to surface low-disk
// warnings. Disk usage is static within a task, so cache it with a 60s TTL to
// avoid blocking the event loop on a subprocess spawn every 30 seconds.
let _dfCacheTimestamp = 0;
let _dfCacheDiskFreeMb: number | undefined;
let _dfCacheDiskTotalMb: number | undefined;
const DF_CACHE_TTL_MS = 60_000;

function _readDiskMetrics(): { disk_free_mb?: number; disk_total_mb?: number } {
  const now = Date.now();
  if (_dfCacheTimestamp > 0 && now - _dfCacheTimestamp < DF_CACHE_TTL_MS) {
    return { disk_free_mb: _dfCacheDiskFreeMb, disk_total_mb: _dfCacheDiskTotalMb };
  }
  try {
    const dfOut = execFileSync('df', ['-k', '/tmp'], { timeout: 5000 }).toString();
    const line = dfOut.split('\n')[1];
    if (line) {
      const parts = line.trim().split(/\s+/);
      _dfCacheDiskTotalMb = Math.round(parseInt(parts[1], 10) / 1024);
      _dfCacheDiskFreeMb = Math.round(parseInt(parts[3], 10) / 1024);
      _dfCacheTimestamp = now;
      return { disk_free_mb: _dfCacheDiskFreeMb, disk_total_mb: _dfCacheDiskTotalMb };
    }
  } catch { /* disk metrics are best-effort */ }
  return {};
}

type MetricsResult = { rss_mb: number; heap_used_mb: number; cgroup_mb?: number; cgroup_limit_mb?: number; disk_free_mb?: number; disk_total_mb?: number };

function _memoryUsageBase(): Pick<MetricsResult, 'rss_mb' | 'heap_used_mb'> {
  const mem = process.memoryUsage();
  return {
    rss_mb: Math.round(mem.rss / 1024 / 1024 * 10) / 10,
    heap_used_mb: Math.round(mem.heapUsed / 1024 / 1024 * 10) / 10,
  };
}

// Sync version for crash handler and sendHeartbeatSync (sync-only contexts).
function collectMetrics(): MetricsResult {
  const { rss_mb, heap_used_mb } = _memoryUsageBase();
  // Read cgroup memory (includes agent + Claude Code + all subprocesses).
  // The agent's own RSS (93MB) hides the real picture — Claude Code can use
  // several GB. MemoryMax=6G triggers cgroup OOM killer (SIGKILL, no handler).
  let cgroup_mb: number | undefined;
  let cgroup_limit_mb: number | undefined;
  try {
    const current = fs.readFileSync('/sys/fs/cgroup/memory.current', 'utf-8').trim();
    cgroup_mb = Math.round(parseInt(current, 10) / 1024 / 1024);
    const max = fs.readFileSync('/sys/fs/cgroup/memory.max', 'utf-8').trim();
    if (max !== 'max') cgroup_limit_mb = Math.round(parseInt(max, 10) / 1024 / 1024);
  } catch { /* cgroup v2 not available */ }
  const { disk_free_mb, disk_total_mb } = _readDiskMetrics();
  return { rss_mb, heap_used_mb, cgroup_mb, cgroup_limit_mb, disk_free_mb, disk_total_mb };
}

// Async version for heartbeat ticks — avoids blocking the event loop on sysfs reads.
async function collectMetricsAsync(): Promise<MetricsResult> {
  const { rss_mb, heap_used_mb } = _memoryUsageBase();
  let cgroup_mb: number | undefined;
  let cgroup_limit_mb: number | undefined;
  try {
    const current = (await fs.promises.readFile('/sys/fs/cgroup/memory.current', 'utf-8')).trim();
    cgroup_mb = Math.round(parseInt(current, 10) / 1024 / 1024);
    const max = (await fs.promises.readFile('/sys/fs/cgroup/memory.max', 'utf-8')).trim();
    if (max !== 'max') cgroup_limit_mb = Math.round(parseInt(max, 10) / 1024 / 1024);
  } catch { /* cgroup v2 not available */ }
  const { disk_free_mb, disk_total_mb } = _readDiskMetrics();
  return { rss_mb, heap_used_mb, cgroup_mb, cgroup_limit_mb, disk_free_mb, disk_total_mb };
}

// Mutex to prevent overlapping heartbeat HTTP calls. Without this, a slow
// heartbeat (apiPost 30s timeout × 5 retries with backoffs = up to ~3min) lets
// subsequent setInterval ticks queue up async invocations behind it. They
// can't deliver until the previous one settles, but they hold microtask state
// and add pressure on the event loop. With the mutex the next tick simply
// skips and logs — last_heartbeat at the orchestrator stays correct because
// the previous in-flight call still finishes and reports.
//
// DEADLINE ESCAPE (durability): the mutex above assumes the in-flight POST
// eventually settles. If one ever hangs (e.g. a stalled response body — see the
// apiRequest body-phase timeout), the flag latches `true` forever and EVERY
// subsequent tick skips, silencing both the rich heartbeat AND the off-row
// vmcontact liveness ping → the reaper false-kills a live VM as AGENT_TIMEOUT
// (the 1474c03c failure mode). So a tick that finds the slot held longer than
// HEARTBEAT_INFLIGHT_DEADLINE treats the prior POST as wedged and reclaims it.
// A `generation` token stops a late-settling wedged POST from clobbering the
// reclaiming tick's own in-flight flag.
let heartbeatInFlight = false; // @per-role
let heartbeatInFlightSince = 0; // @per-role
let heartbeatGeneration = 0;

function startHeartbeat(taskId: string, getMessage: () => string) {
  // Fire first heartbeat immediately — don't wait 30s.
  // If the agent crashes in the first 30s, at least one heartbeat is recorded.
  // Audit 2026-04-25 (W4/C1+C2): replace `void async` IIFE with explicit
  // .catch() handler so a synchronous throw inside the async block (e.g.
  // collectMetrics() bug, JSON.stringify failure) doesn't silently disappear.
  // The previous `void` form would drop those errors entirely — no log, no
  // unhandledRejection event, the agent just goes silent until the orchestrator
  // reaper kills it minutes later with no forensic record.
  (async () => {
    const metrics = await collectMetricsAsync();
    const fix_loop_events = collectFixLoopEventsForFlush();
    const statusMessage = getMessage();
    const tool_trace = snapshotToolTrace({ refreshLastEntry: statusMessage.startsWith('Using tool:') });
    const start = Date.now();
    try {
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: `[${metrics.heap_used_mb}MB] ${sanitize(statusMessage)}`,
        metrics,
        tool_trace,
        ...(fix_loop_events.length > 0 ? { fix_loop_events } : {}),
        ...(getCurrentPromptFingerprint() ? { prompt_fingerprint: getCurrentPromptFingerprint() } : {}),
        ...(agentRuntimeFingerprint ? { agent_runtime_fingerprint: agentRuntimeFingerprint } : {}),
        ...(agentSourceSha ? { agent_source_sha: agentSourceSha } : {}),
        ...getLlmTelemetry(),
        ...getFormatComplianceTelemetry(),
      }, HEARTBEAT_API_MAX_RETRIES, HEARTBEAT_API_TIMEOUT);
      traceHeartbeat('initial', start, Date.now(), 'ok');
    } catch (e) {
      log(`Initial heartbeat failed: ${String(e)}`);
      traceHeartbeat('initial', start, Date.now(), 'err', String(e));
      if (fix_loop_events.length > 0) requeueFixLoopEvents(fix_loop_events);
      // Terminal-task 409 on initial heartbeat: task cancelled/blocked before the
      // agent even had a chance to run. Self-terminate the same way the periodic
      // heartbeat would so we don't keep spinning on work that can't proceed.
      if (e instanceof Error && /failed \(409\)/.test(e.message) && /already terminal/i.test(e.message)) {
        log('Task already terminal on initial heartbeat — self-terminating gracefully');
        requestCancel();
        stopHeartbeat();
        postTerminalStatusSync(taskId, 'timeout', 'Task became terminal before first heartbeat — agent self-terminating after 409 task-terminal signal');
        setTimeout(() => process.exit(0), 5000);
      }
    }
  })().catch((e: unknown) => { log(`Initial heartbeat outer error: ${String(e)}`); });

  heartbeatTimer = setInterval(() => { (async () => {
    // A prior tick already classified a non-transient 4xx as reassignment and
    // requested shutdown — don't keep firing heartbeats (or re-trip the
    // self-terminate path) while the 5s wind-down timer runs.
    if (cancelRequested) {
      traceHeartbeat('periodic', Date.now(), Date.now(), 'skip-in-flight', 'cancel-requested');
      return;
    }
    if (heartbeatInFlight) {
      const heldMs = Date.now() - heartbeatInFlightSince;
      if (heldMs <= HEARTBEAT_INFLIGHT_DEADLINE) {
        traceHeartbeat('periodic', Date.now(), Date.now(), 'skip-in-flight');
        log('Heartbeat skipped — previous still in flight');
        return;
      }
      // Held past the deadline — the prior POST is wedged (should be impossible
      // now that apiRequest bounds the body read, but this keeps liveness
      // un-latchable regardless). Reclaim the slot so heartbeats resume; the
      // generation bump below stops the wedged POST's `finally` from clearing
      // the new tick's flag when it eventually settles.
      log(`Heartbeat in-flight ${Math.round(heldMs / 1000)}s > deadline — reclaiming slot (prior POST wedged)`);
      traceHeartbeat('periodic', Date.now(), Date.now(), 'reclaim-wedged');
    }
    heartbeatInFlight = true;
    heartbeatInFlightSince = Date.now();
    const myGeneration = ++heartbeatGeneration;
    const start = Date.now();
    const metrics = await collectMetricsAsync();
    // Proactive OOM warning — SIGKILL from cgroup can't be caught, so warn while we still can
    if (metrics.cgroup_mb && metrics.cgroup_limit_mb) {
      const memPct = metrics.cgroup_mb / metrics.cgroup_limit_mb;
      if (memPct > 0.85) {
        log(`WARNING: Memory at ${(memPct * 100).toFixed(0)}% of cgroup limit (${metrics.cgroup_mb}MB / ${metrics.cgroup_limit_mb}MB) — OOM kill imminent`);
      }
    }
    // Low disk warning — git/npm operations fail silently when /tmp is full
    if (metrics.disk_free_mb !== undefined && metrics.disk_free_mb < 512) {
      log(`WARNING: Low disk space: ${metrics.disk_free_mb}MB free on /tmp`);
    }
    const fix_loop_events = collectFixLoopEventsForFlush();
    const statusMessage = getMessage();
    const tool_trace = snapshotToolTrace({ refreshLastEntry: statusMessage.startsWith('Using tool:') });
    try {
      // Tighter heartbeat-specific retry/timeout (10s × 2 vs default 30s × 5).
      // Holding the in-flight mutex through the full 150s+ default chain on a
      // sustained 5xx run starves subsequent setInterval ticks — so the
      // orchestrator never sees a fresh heartbeat even after it recovers.
      // Tighter values keep the mutex turning over.
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: `[${metrics.heap_used_mb}MB] ${sanitize(statusMessage)}`,
        metrics,
        tool_trace,
        ...(fix_loop_events.length > 0 ? { fix_loop_events } : {}),
        ...(getCurrentPromptFingerprint() ? { prompt_fingerprint: getCurrentPromptFingerprint() } : {}),
        ...(agentRuntimeFingerprint ? { agent_runtime_fingerprint: agentRuntimeFingerprint } : {}),
        ...(agentSourceSha ? { agent_source_sha: agentSourceSha } : {}),
        ...getLlmTelemetry(),
        ...getFormatComplianceTelemetry(),
      }, HEARTBEAT_API_MAX_RETRIES, HEARTBEAT_API_TIMEOUT);
      traceHeartbeat('periodic', start, Date.now(), 'ok');
    } catch (e: unknown) {
      log(`Heartbeat failed: ${String(e)}`);
      traceHeartbeat('periodic', start, Date.now(), 'err', String(e));
      // Re-queue drained events so a transient POST failure doesn't silently
      // lose this tick's fix-loop telemetry. The next heartbeat (or the
      // terminal status post) carries them.
      if (fix_loop_events.length > 0) requeueFixLoopEvents(fix_loop_events);
      // 409 = two distinct backend signals:
      //   "Task already terminal" — task was cancelled/failed/blocked externally.
      //     Self-terminate immediately (same graceful path as reassignment 401/403).
      //   "Task row locked" (skip_locked) — row held by concurrent status write.
      //     Retry once after 5s; silently dropping can let last_heartbeat go stale.
      if (e instanceof Error && /failed \(409\)/.test(e.message)) {
        if (/already terminal/i.test(e.message)) {
          log('Task became terminal externally — self-terminating gracefully after heartbeat 409');
          requestCancel();
          stopHeartbeat();
          postTerminalStatusSync(taskId, 'timeout', 'Task became terminal (cancelled/blocked/completed) externally — agent self-terminating after heartbeat 409 task-terminal signal');
          setTimeout(() => process.exit(0), 5000);
          return;
        }
        // Exponential backoff: 5s, then 10s. Total window 15s, within 30s interval.
        for (let retry = 0; retry < 2; retry++) {
          const delay = 5000 * (retry + 1);
          log(`Heartbeat skipped (row locked), retrying in ${delay / 1000}s (attempt ${retry + 1}/2)`);
          await new Promise(r => setTimeout(r, delay));
          const retryStart = Date.now();
          try {
            const retryMetrics = await collectMetricsAsync();
            const retryStatusMessage = getMessage();
            const retryToolTrace = snapshotToolTrace({ refreshLastEntry: retryStatusMessage.startsWith('Using tool:') });
            await apiPost('/api/agent/heartbeat', {
              task_id: taskId,
              status_message: `[${retryMetrics.heap_used_mb}MB] ${sanitize(retryStatusMessage)}`,
              metrics: retryMetrics,
              tool_trace: retryToolTrace,
              ...(getCurrentPromptFingerprint() ? { prompt_fingerprint: getCurrentPromptFingerprint() } : {}),
              ...getLlmTelemetry(),
              ...getFormatComplianceTelemetry(),
            }, HEARTBEAT_API_MAX_RETRIES, HEARTBEAT_API_TIMEOUT);
            log('Heartbeat retry succeeded');
            traceHeartbeat('retry-409', retryStart, Date.now(), 'ok');
            return;
          } catch (retryErr: unknown) {
            log(`Heartbeat retry ${retry + 1}/2 failed: ${String(retryErr)}`);
            traceHeartbeat('retry-409', retryStart, Date.now(), 'err', String(retryErr));
          }
        }
        return;
      }
      // Transient 4xx (429 rate-limited, 408/425 timeout) — NOT a reassignment.
      // Back off and keep heartbeating; the reaper's no-heartbeat window is the
      // backstop. Regression 2026-05-29: a transient 429 here self-terminated
      // live agents mid-task (recorded as AGENT_TIMEOUT) under orchestrator load.
      // (409 row-locked is handled by its own retry branch above.)
      if (e instanceof Error && isHeartbeatTransient(e.message)) {
        log('Heartbeat transient failure (rate-limited/timeout) — backing off, will retry next interval');
        return;
      }
      // Other 4xx (401/403/404 …) = task genuinely reassigned/deauthorized — graceful shutdown
      if (e instanceof Error && /failed \(4\d{2}\)/.test(e.message)) {
        log('Task no longer assigned to this VM — self-terminating gracefully');
        // Signal the rest of the agent (incl. any later heartbeat tick that has
        // already passed the classification gate) that we are shutting down.
        requestCancel();
        stopHeartbeat();
        // Best-effort terminal status POST BEFORE exiting. Without this the
        // task is stranded IN_PROGRESS/QUEUED with no failure_category until
        // the reaper times it out (false AGENT_TIMEOUT, discarded work). The
        // heartbeat callback is a detached setInterval tick — a thrown
        // GracefulShutdownSignal can't reach main()'s role loop from here — so
        // fire a fire-and-forget SYNC status post (curl child, same mechanism
        // as sendHeartbeatSync) that survives the imminent process.exit even
        // if the event loop never drains. 'reassigned' tells the backend this
        // was an intentional handoff, not a code fault.
        postTerminalStatusSync(taskId, 'reassigned', 'Task reassigned/deauthorized — agent self-terminating after non-transient heartbeat 4xx');
        // Allow 5s for in-flight operations to wind down before hard exit.
        // VM TTL self-destruct (1hr) is the ultimate backstop.
        setTimeout(() => process.exit(0), 5000);
        return;
      }
    } finally {
      // Only release the slot if a later tick hasn't already reclaimed it (i.e.
      // this POST wasn't declared wedged and superseded). Without the guard, a
      // wedged POST settling late would clear the reclaiming tick's flag and let
      // a third overlap through.
      if (heartbeatGeneration === myGeneration) heartbeatInFlight = false;
    }
  })().catch((e: unknown) => { log(`Heartbeat outer error (W4/C1): ${String(e)}`); }); }, HEARTBEAT_INTERVAL);
}

function stopHeartbeat() {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
  stopProactiveTokenRefresh();
  stopGithubTokenRefresh();
  // Reset mutex so warm-VM continuity (one agent process, multiple sequential
  // tasks) starts the next task with a clean in-flight state. Without this,
  // a stuck heartbeat at role-end would leave the flag true and the new
  // task's first periodic tick would skip-and-log instead of firing.
  // (heartbeatGeneration is intentionally left monotonic — it only needs to be
  // unique per tick, never reset.)
  heartbeatInFlight = false;
  heartbeatInFlightSince = 0;
}

/**
 * Build the heartbeat status string from a statusRef, appending in-tool elapsed time
 * and last-progress age when a tool is in flight.
 */
function buildHeartbeatStatus(
  statusRef: { value: string; toolStartedAt?: number; lastProgressAt?: number },
  now: number,
): string {
  const base = statusRef.value;
  if (statusRef.toolStartedAt) {
    const elapsedSec = Math.floor((now - statusRef.toolStartedAt) / 1000);
    const elapsedStr = elapsedSec >= 60
      ? `${Math.floor(elapsedSec / 60)}m ${elapsedSec % 60}s`
      : `${elapsedSec}s`;
    const progressPart = statusRef.lastProgressAt
      ? `, last sub-progress ${Math.floor((now - statusRef.lastProgressAt) / 1000)}s ago`
      : '';
    return `${base} (${elapsedStr} elapsed${progressPart})`;
  }
  return base;
}

/** Test-only: build the heartbeat status string for a given statusRef and timestamp. */
export function _buildHeartbeatStatusForTesting(
  statusRef: { value: string; toolStartedAt?: number; lastProgressAt?: number },
  now: number,
): string {
  return buildHeartbeatStatus(statusRef, now);
}

/** Test-only: drive the heartbeat loop without running main(). */
export function _startHeartbeatForTesting(taskId: string, getMessage: () => string): void {
  startHeartbeat(taskId, getMessage);
}

/** Test-only: stop the heartbeat loop between unit tests. */
export function _stopHeartbeatForTesting(): void {
  stopHeartbeat();
}

/** Synchronous heartbeat — use between blocking execFileSync calls to prevent reaper from killing us.
 *  Fires curl in a child process (sync) so the event loop doesn't need to be free.
 *  `timeoutMs` defaults to 10s for normal between-step heartbeats (git clone
 *  retry, npm ci, push, etc — degraded networks need room). Signal-handler
 *  callers (`_crashReport` SIGTERM/uncaughtException paths) pass 3s instead so
 *  one slow curl can't chew the GCE 30s grace window before SIGKILL. */
export function sendHeartbeatSync(
  taskId: string,
  statusMessage: string,
  extras?: Record<string, unknown>,
  timeoutMs = 10_000,
) {
  const start = Date.now();
  try {
    const metrics = collectMetrics();
    const payload = JSON.stringify({
      task_id: taskId,
      status_message: `[${metrics.heap_used_mb}MB] ${sanitize(statusMessage)}`,
      metrics,
      ...getLlmTelemetry(),
      ...getFormatComplianceTelemetry(),
      ...(extras || {}),
    });
    execFileSync('curl', [
      '-sf', '-X', 'POST',
      `${ORCHESTRATOR_URL}/api/agent/heartbeat`,
      '-H', 'Content-Type: application/json',
      '-H', `X-Agent-Token: ${AGENT_TOKEN}`,
      '-d', payload,
    ], { timeout: timeoutMs, stdio: 'ignore' });
    traceHeartbeat('sync', start, Date.now(), 'ok');
  } catch (e) {
    log(`Sync heartbeat failed: ${e instanceof Error ? e.message : String(e)} (non-fatal)`);
    traceHeartbeat('sync', start, Date.now(), 'err', e instanceof Error ? e.message : String(e));
  }
}

/**
 * Synchronous, fire-and-forget terminal status POST — used from the heartbeat
 * self-terminate path (cluster C7) where a thrown GracefulShutdownSignal can't
 * reach main()'s role loop (the heartbeat is a detached setInterval tick) and
 * the imminent process.exit would abandon any pending async apiPost. Fires curl
 * in a child process (sync) so the request lands even though the event loop is
 * about to stop. Without this the reassigned task is stranded IN_PROGRESS with
 * no failure_category until the reaper times it out (false AGENT_TIMEOUT).
 *
 * Best-effort: a failed POST writes the stale-result file as the fallback
 * (the next process / warm role retries it), mirroring persistAndPostStatus.
 */
export function postTerminalStatusSync(
  taskId: string,
  failureCategory: FailureCategory,
  message: string,
  timeoutMs = 5_000,
) {
  if (!taskId) return;
  const enriched = enrichStatusPayload(taskId, {
    task_id: taskId,
    status: 'FAILED',
    artifact_type: 'ERROR_LOG',
    logs: sanitize(message),
    failure_category: failureCategory,
  });
  // Persist a stale-result file FIRST so a retry path survives even if the
  // curl below times out under a partition.
  try {
    writeStaleResultFile(`/tmp/task_${taskId}_result.json`, createPersistedStaleResultEnvelope(enriched));
  } catch { /* best effort — directory may be unwritable */ }
  try {
    execFileSync('curl', [
      '-sf', '-X', 'POST',
      `${ORCHESTRATOR_URL}/api/agent/status`,
      '-H', 'Content-Type: application/json',
      '-H', `X-Agent-Token: ${AGENT_TOKEN}`,
      '-d', JSON.stringify(enriched),
    ], { timeout: timeoutMs, stdio: 'ignore' });
    // POST landed — drop the stale-result fallback so it isn't re-posted.
    try { fs.unlinkSync(`/tmp/task_${taskId}_result.json`); } catch { /* already gone */ }
    log(`Terminal status (${failureCategory}) posted synchronously for task ${taskId}`);
  } catch (e) {
    log(`Sync terminal status POST failed: ${e instanceof Error ? e.message : String(e)} — stale-result fallback retained for retry`);
  }
}

// ── WebSocket Log Stream ────────────────────────────────────────────────────
interface LogEntry { timestamp: number; content: string; }
const logBuffer: LogEntry[] = [];

export function appendToLogBuffer(content: string) {
  if (logBuffer.length >= MAX_LOG_BUFFER) logBuffer.shift();
  logBuffer.push({ timestamp: Date.now(), content });
}

export function snapshotLogBuffer(): LogEntry[] {
  return logBuffer.slice();
}

export function clearLogBuffer(): void {
  logBuffer.splice(0);
}

// Replay the log buffer at a rate ≤15 msgs/s to stay safely under the
// backend's 20 msg/s close-threshold (routers/projects.py).
export async function replayLogBufferRateLimited(ws: WebSocket): Promise<void> {
  const BATCH = 15;
  while (logBuffer.length > 0) {
    const batchStart = Date.now();
    let sent = 0;
    while (logBuffer.length > 0 && sent < BATCH) {
      const entry = logBuffer[0];
      try { ws.send(JSON.stringify({ type: 'log', content: entry.content })); } catch { return; }
      logBuffer.shift();
      sent++;
    }
    if (logBuffer.length > 0) {
      const elapsed = Date.now() - batchStart;
      if (elapsed < 1000) await new Promise(r => setTimeout(r, 1000 - elapsed));
    }
  }
}

async function connectLogStream(taskId: string): Promise<WebSocket | null> {
  const wsUrl = ORCHESTRATOR_URL.replace(/^http/, 'ws');
  const maxRetries = 3;
  const baseDelay = 2000;
  let lastFailure = 'unknown';

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      const ws = await new Promise<WebSocket | null>((resolve) => {
        const conn = new WebSocket(`${wsUrl}/api/agent/ws/${taskId}`);
        let settled = false;
        const finish = (socket: WebSocket | null, failure?: string) => {
          if (settled) return;
          settled = true;
          if (failure) lastFailure = failure;
          resolve(socket);
        };
        const timeout = setTimeout(() => {
          try { conn.terminate(); } catch { /* ignore */ }
          finish(null, 'connect timeout');
        }, 5000);
        conn.on('open', () => {
          clearTimeout(timeout);
          conn.send(JSON.stringify({ token: AGENT_TOKEN }));
          finish(conn);
        });
        conn.on('error', (err: Error) => {
          clearTimeout(timeout);
          finish(null, err.message || 'websocket error');
        });
      });
      if (ws) {
        await replayLogBufferRateLimited(ws);
        return ws;
      }
    } catch (err) {
      lastFailure = err instanceof Error ? err.message : String(err);
    }
    if (attempt < maxRetries - 1) {
      await new Promise(r => setTimeout(r, baseDelay * Math.pow(2, attempt)));
    }
  }
  setPendingStatusFlag(taskId, 'websocket_connect_failed', true);
  setPendingStatusFlag(taskId, 'websocket_connect_error', sanitize(lastFailure).slice(0, 500));
  setPendingStatusFlag(taskId, 'websocket_connect_attempts', maxRetries);
  return null;
}

export { sanitize, sanitizeError };

export function readHandlerOutput(
  taskId: string,
  outputType: string,
  ws: WebSocket | null,
): unknown {
  const filePath = `/tmp/${outputType}_${taskId}.json`;
  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err: unknown) {
    sendLog(ws, `[WARNING] Failed to read ${filePath}: ${err instanceof Error ? err.message : String(err)}\n`);
    return null;
  }
  try {
    return JSON.parse(raw) as unknown;
  } catch (err: unknown) {
    sendLog(ws, `[WARNING] Failed to parse ${filePath}: ${err instanceof Error ? err.message : String(err)}\n`);
    // Try extracting a JSON array (research findings, plans)
    // Only accept non-empty arrays; empty arrays (e.g. `[]` embedded inside an
    // object like `"direction_operations": []`) would otherwise shadow the
    // subsequent object extraction.
    const arrayResult = extractJsonArray(raw);
    if (arrayResult && Array.isArray(arrayResult) && arrayResult.length > 0) return arrayResult;
    // Try extracting a JSON object (strategic output)
    try {
      const objResult = extractJsonObject(raw, 'vision', 'direction_operations');
      if (objResult) return objResult;
    } catch (extractErr: unknown) {
      if (extractErr instanceof JsonExtractionError) {
        sendLog(ws, `[ERROR] ${extractErr.message}\n`);
      }
      throw extractErr;
    }
    return null;
  }
}

export function validateBranchName(branch: string, label: string): string {
  if (!branch) throw new Error(`${label} is required`);
  if (!/^[a-zA-Z0-9_./-]+$/.test(branch)) throw new Error(`Invalid ${label}: ${branch}`);
  if (branch.includes('..')) throw new Error(`Invalid ${label} (contains ..): ${branch}`);
  if (branch.startsWith('-')) throw new Error(`Invalid ${label} (starts with -): ${branch}`);
  return branch;
}

// Canonical task-type → branch-prefix mapping.
// Convention: {namespace}/{prefix}-{first-8-hex-of-task-uuid}
// Mirrors backend/app/shared_state.py TASK_TYPE_BRANCH_PREFIX (Python single source); keep in sync.
// Types absent from this map default to the 'task' prefix.
export const TASK_TYPE_BRANCH_PREFIX: Record<string, string> = {
  refactor: 'refactor',
  docs: 'docs',
  deps: 'deps',
  migrate: 'migrate',
};

/** Canonical task-branch namespace for the Blueprint self-repo.  This is NOT a
 *  silent default for {@link deriveTaskBranch}; it is only the terminal
 *  fallback callers apply when ``buildConfig.task_branch_namespace`` is absent
 *  (mirrors the backend's ``get_project_task_branch_namespace`` resolver). */
export const TASK_BRANCH_NAMESPACE_DEFAULT = 'blueprint';

/** Resolve the per-project task-branch namespace from a raw task record.
 *  Reads ``task.build_config.task_branch_namespace`` DIRECTLY — deliberately
 *  NOT via {@link resolveBuildConfig}, which returns undefined for a project
 *  with no build directories (greenfield / docs-only external repos). Routing
 *  the namespace through that resolver silently dropped it and defaulted every
 *  branch to "blueprint/...", diverging from the orchestrator-derived branch
 *  and orphaning the PR (the namespace-divergence bug). Namespace resolution
 *  must NOT depend on the directories gate. The slug is validated ([a-z0-9-],
 *  matching the backend ``_derive_task_branch_namespace`` slugifier); an absent
 *  or malformed value falls back to the canonical Blueprint default. Mirrors
 *  the backend ``get_project_task_branch_namespace``. Callers thread the result
 *  into {@link deriveTaskBranch} rather than relying on a hidden default. */
export function resolveTaskBranchNamespace(
  task: Record<string, unknown> | null | undefined,
): string {
  const buildConfig = task?.build_config as Record<string, unknown> | undefined;
  const ns = buildConfig?.task_branch_namespace;
  if (typeof ns === 'string' && /^[a-z0-9][a-z0-9-]{0,39}$/.test(ns)) return ns;
  return TASK_BRANCH_NAMESPACE_DEFAULT;
}

/** Return the canonical git branch name for a task.
 *
 *  ``namespace`` is REQUIRED — there is no silent ``"blueprint"`` default, so
 *  every call site for an arbitrary repo must thread through the project's
 *  actual namespace (resolve it via {@link resolveTaskBranchNamespace} from
 *  ``buildConfig.task_branch_namespace``).  Mirrors the backend
 *  ``derive_task_branch``. */
export function deriveTaskBranch(
  taskType: string,
  taskId: string,
  namespace: string,
): string {
  const prefix = TASK_TYPE_BRANCH_PREFIX[taskType] ?? 'task';
  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  return `${namespace}/${prefix}-${shortId}`;
}

export function sendLog(ws: WebSocket | null, content: string) {
  const sanitized = sanitize(content);
  appendToLogBuffer(sanitized);
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: 'log', content: sanitized })); } catch { /* ignore */ }
  } else {
    process.stdout.write(sanitized);
  }
}

/**
 * Send an arbitrary typed WebSocket frame to the live viewer socket (e.g. the
 * resident strategist's `{type:"strategic_session", payload:{…}}` transcript
 * deltas the backend maps to `emit_strategic_session_delta`). Returns true when
 * the frame went out on an OPEN socket; false when there is no live socket, so
 * the caller can fall back to a buffered `sendLog`. Centralizes the same
 * readyState guard `sendLog` uses so callers never touch `WebSocket.OPEN`.
 */
export function sendWsFrame(ws: WebSocket | null, frame: Record<string, unknown>): boolean {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(frame)); return true; } catch { /* ignore */ }
  }
  return false;
}

// ── Git Helpers ─────────────────────────────────────────────────────────────
export function git(args: string[], cwd = WORKSPACE): string {
  return execFileSync('git', args, { cwd, encoding: 'utf-8', timeout: 120_000 }).trim();
}

/**
 * Register the `sizecap` custom merge driver in the workspace's LOCAL git config.
 *
 * `.gitattributes` maps `backend/testing/module_size_limits.json` to
 * `merge=sizecap`, but git ignores that mapping unless the driver command is
 * registered in the repo's `.git/config` — and a clone does NOT carry git config.
 * So every workspace-prep site (cold clone, warm reuse, setupWorkspace) must call
 * this so a rebase/merge of competing size-cap bumps auto-resolves with no markers
 * (max-cap union; see backend/testing/merge_size_limits.py). Idempotent — git
 * config overwrites the keys on repeat calls. The driver runs from the worktree
 * root, so the script path is repo-relative. Best-effort: a failure here only
 * means the JSON falls back to the normal conflict path, so we log and continue.
 */
export function registerSizeCapMergeDriver(workspace = WORKSPACE): void {
  try {
    execFileSync(
      'git',
      ['-C', workspace, 'config', 'merge.sizecap.name', 'max-cap union merge for module_size_limits.json'],
      { encoding: 'utf-8', timeout: 30_000 },
    );
    execFileSync(
      'git',
      ['-C', workspace, 'config', 'merge.sizecap.driver', 'python3 backend/testing/merge_size_limits.py %O %A %B'],
      { encoding: 'utf-8', timeout: 30_000 },
    );
  } catch (e) {
    log(`registerSizeCapMergeDriver: failed to register sizecap merge driver: ${String(e)}`);
  }
}

/** Non-blocking exec that mirrors execFileSync's error shape.
 *  Use this for long-running commands (test runners, build tools) so the
 *  event loop stays free and the setInterval heartbeat keeps firing.
 *  On timeout, sends SIGTERM to the whole process group, then SIGKILL 5s
 *  later — this terminates npm/pytest worker processes that execFileSync's
 *  default signal handling leaks (stuck children block the sync call past
 *  its timeout, starving the heartbeat and tripping the reaper). */
export function execFileAsync(
  cmd: string,
  args: string[],
  opts: { cwd?: string; timeoutMs?: number; env?: NodeJS.ProcessEnv } = {},
): Promise<string> {
  const { cwd, timeoutMs = 180_000, env } = opts;
  return new Promise((resolve, reject) => {
    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];
    // `timeout` is not a valid option for child_process.spawn (only execFile* and
    // spawnSync respect it), but Node silently ignores unknown options. We pass
    // it through so tests can verify the configured timeout per runner via the
    // spawn mock's call arguments. The actual timeout logic is handled by the
    // termTimer setInterval below.
    const proc = spawn(cmd, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true, timeout: timeoutMs } as Parameters<typeof spawn>[2]);
    // Phase 1: register the child's PID so a reset between roles can SIGKILL
    // a stuck test runner. Long-running by design (timeoutMs default 180s);
    // short utility execFile calls (git log, etc.) go through execFileSync
    // and are not tracked here.
    if (typeof proc.pid === 'number') {
      registerChildPid(proc.pid);
    }
    if (!proc.stdout || !proc.stderr) throw new Error('spawn returned no stdio streams');
    proc.stdout.on('data', (d: Buffer) => stdoutChunks.push(d));
    proc.stderr.on('data', (d: Buffer) => stderrChunks.push(d));
    let timedOut = false;
    let killTimer: NodeJS.Timeout | null = null;
    const originalPid = proc.pid;
    const processGroupStillOwned = () =>
      originalPid !== undefined
      && proc.pid === originalPid
      && proc.exitCode === null
      && proc.signalCode === null;
    const termTimer = setTimeout(() => {
      timedOut = true;
      const pid = originalPid;
      if (pid !== undefined && processGroupStillOwned()) {
        try { process.kill(-pid, 'SIGTERM'); } catch { /* already exited */ }
        killTimer = setTimeout(() => {
          if (!processGroupStillOwned()) return;
          try { process.kill(-pid, 'SIGKILL'); } catch { /* already dead */ }
        }, PROCESS_KILL_GRACE_MS);
      }
    }, timeoutMs);
    proc.on('close', (code, signal) => {
      const closeSignal = signal ?? null;
      clearTimeout(termTimer);
      if (killTimer) clearTimeout(killTimer);
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      const stdout = Buffer.concat(stdoutChunks).toString('utf-8');
      const stderr = Buffer.concat(stderrChunks).toString('utf-8');
      if (code === 0 && closeSignal === null && !timedOut) {
        resolve(stdout);
        return;
      }
      const parts = [`Command failed: ${cmd} ${args.join(' ')}`];
      if (closeSignal) parts.push(`Signal: ${closeSignal}`);
      if (stdout.trim()) parts.push(stdout);
      if (stderr.trim()) parts.push(stderr);
      const err: Error & {
        stdout?: string; stderr?: string; status?: number | null; signal?: NodeJS.Signals | null; killed?: boolean; code?: string;
      } = new Error(parts.join('\n'));
      err.stdout = stdout;
      err.stderr = stderr;
      err.status = code;
      err.signal = closeSignal;
      err.killed = timedOut || closeSignal !== null;
      if (closeSignal === 'SIGKILL' && !timedOut) {
        err.code = 'EOOM';
      } else if (timedOut) {
        err.code = 'ETIMEDOUT';
      }
      reject(err);
    });
    proc.on('error', (e) => {
      clearTimeout(termTimer);
      if (killTimer) clearTimeout(killTimer);
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      reject(e);
    });
  });
}

/** Async git that doesn't block the event loop — use for long operations (push, clone)
 *  so the setInterval heartbeat can keep firing. */
export function gitAsync(args: string[], cwd = WORKSPACE, timeoutMs = 300_000): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    const errChunks: Buffer[] = [];
    const proc = spawn('git', args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    proc.stdout.on('data', (d: Buffer) => chunks.push(d));
    proc.stderr.on('data', (d: Buffer) => errChunks.push(d));
    const timer = setTimeout(() => { proc.kill('SIGKILL'); reject(new Error(`git ${args[0]} timed out after ${timeoutMs}ms`)); }, timeoutMs);
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) resolve(Buffer.concat(chunks).toString('utf-8').trim());
      else reject(new Error(`git ${args.join(' ')} failed (exit ${code}): ${Buffer.concat(errChunks).toString('utf-8').trim()}`));
    });
    proc.on('error', (e) => { clearTimeout(timer); reject(e); });
  });
}

/** Git authentication failures (as opposed to transient network/DNS errors).
 *  An expired/invalid installation token surfaces as one of these. We must be
 *  precise: a network or DNS failure is NOT an auth error and must keep today's
 *  plain-retry behavior (re-minting a token would not help and a successful
 *  re-mint could mask a real connectivity problem). */
const GIT_CLONE_AUTH_ERROR = /Invalid username or token|Password authentication is not supported|Authentication failed|remote: (?:Invalid|Repository not found)|returned error:\s*40[13]\b|HTTP\s*40[13]\b/i;

/** Re-mint a fresh GitHub installation token (force_refresh busts the backend's
 *  ~50min cache), write it to the credential-helper token file, and also rewrite
 *  any `x-access-token:<tok>@github.com` segment in-place for legacy callers
 *  that still embed the token in the URL. Returns true if a fresh token was
 *  obtained (regardless of whether a URL was rewritten). */
async function refreshCloneToken(args: string[], taskId: string): Promise<boolean> {
  if (!taskId) return false;
  const freshRes = await apiGet(
    `/api/agent/github/token?task_id=${taskId}&force_refresh=true`,
    API_MAX_RETRIES, 180_000,
  ) as { data?: { token?: string } };
  const freshToken = freshRes.data?.token;
  if (!freshToken) return false;
  // Persist the fresh token so the credential helper reads it on the retry.
  writeGitTokenFile(freshToken);
  // Also rewrite any legacy token-embedded URL in the args array so old-style
  // callers that still build `x-access-token:<tok>@github.com` URLs also get
  // the fresh token on their retry attempt.
  const urlIdx = args.findIndex(a => /https:\/\/x-access-token:[^@]+@github\.com\//.test(a));
  if (urlIdx !== -1) {
    args[urlIdx] = args[urlIdx].replace(
      /(https:\/\/x-access-token:)[^@]+(@github\.com\/)/,
      `$1${freshToken}$2`,
    );
  }
  return true;
}

/** Clone with retry — transient network failures get 3 attempts with exponential
 *  backoff. On an AUTH failure (expired/invalid token) we additionally re-mint a
 *  fresh token and rewrite the clone URL before retrying — a token baked in at
 *  role start can expire during a long queue/provision delay, and every plain
 *  retry would otherwise reuse the same dead token. */
export async function cloneWithRetry(args: string[], timeoutMs = GIT_CLONE_TIMEOUT): Promise<void> {
  const taskId = process.env.TASK_ID || '';
  for (let attempt = 0; attempt < CLONE_MAX_RETRIES; attempt++) {
    try {
      if (attempt > 0 && taskId) log(`Git clone retry ${attempt + 1}/${CLONE_MAX_RETRIES}...`);
      await gitAsync(args, '.', timeoutMs);
      return;
    } catch (e) {
      if (attempt === CLONE_MAX_RETRIES - 1) throw e;
      const rawMsg = e instanceof Error ? e.message : String(e);
      const delay = CLONE_RETRY_BASE_DELAY * Math.pow(2, attempt);
      const isAuthError = GIT_CLONE_AUTH_ERROR.test(rawMsg);
      log(`Git clone failed (attempt ${attempt + 1}/3)${isAuthError ? ' [auth]' : ''}, retrying in ${delay / 1000}s: ${sanitize(rawMsg)}`);
      if (isAuthError) {
        // Re-mint a fresh installation token (the baked-in one likely expired)
        // and rewrite the clone URL before retrying. Never log the token.
        try {
          const rewritten = await refreshCloneToken(args, taskId);
          if (rewritten) log('Re-minted fresh GitHub token after clone auth failure');
        } catch (refreshErr) {
          log(`Token re-mint after clone auth failure failed: ${sanitizeError(refreshErr)}`);
        }
      }
      try { execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' }); } catch { /* ignore */ }
      await new Promise(r => setTimeout(r, delay));
    }
  }
}

/** List open PRs whose head branch matches `branch` on `owner/repo`. */
export async function listOpenPullRequestsForBranch(
  ghToken: string, owner: string, repo: string, branch: string
): Promise<{ number: number; html_url: string }[]> {
  const url = `https://api.github.com/repos/${owner}/${repo}/pulls?state=open&head=${encodeURIComponent(`${owner}:${branch}`)}&per_page=10`;
  const controller = new AbortController();
  const timeout = setTimeout(() => { controller.abort(); }, 30_000);
  try {
    const res = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${ghToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      signal: controller.signal,
    });
    if (!res.ok) {
      log(`listOpenPullRequestsForBranch ${branch} got HTTP ${res.status} — assuming no open PRs`);
      return [];
    }
    const data = await res.json() as Array<{ number: number; html_url: string }>;
    return Array.isArray(data) ? data : [];
  } finally {
    clearTimeout(timeout);
  }
}

// ── Workspace + PR Helpers ───────────────────────────────────────────────────
export async function setupWorkspace(
  owner: string, repo: string, ghToken: string, baseBranch: string,
  branchPrefix: string, taskId: string,
  ws: WebSocket | null, statusRef: { value: string },
  taskBranchNamespace: string,
  buildConfig?: BuildConfig
): Promise<{ workBranch: string; shortId: string }> {
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  sendHeartbeatSync(taskId, `Cloning ${owner}/${repo}...`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  // Write the token to the credential-helper file and ensure the helper script
  // exists before the clone so git can authenticate without an embedded token.
  writeGitTokenFile(ghToken);
  const helperPath = ensureGitCredentialHelperScript();
  const cleanCloneUrl = `https://github.com/${owner}/${repo}.git`;
  const cloneArgs = [
    '-c', `credential.https://github.com.helper=${helperPath}`,
    'clone', '--depth', '100', '--branch', baseBranch,
  ];
  // Reuse the pre-baked git object cache when the image seeded a mirror for
  // this repo. `--reference-if-able` borrows objects from the local mirror and
  // silently falls back to a normal fetch if the path is missing or invalid,
  // so this is safe for repos the image never cached.
  const referenceRepo = `${GIT_REFERENCE_CACHE_ROOT}/${owner}/${repo}.git`;
  if (fs.existsSync(referenceRepo)) {
    cloneArgs.push('--reference-if-able', referenceRepo);
    sendLog(ws, `Using pre-baked git object cache at ${referenceRepo}\n`);
  }
  cloneArgs.push(cleanCloneUrl, WORKSPACE);
  try {
    await cloneWithRetry(cloneArgs);
  } catch (e) {
    const err = e instanceof Error ? e : new Error(String(e));
    (err as Error & { failureCategory?: FailureCategory }).failureCategory = 'clone_failed';
    throw err;
  }
  // Configure the credential helper and clean remote URL in the workspace git
  // config so all subsequent operations authenticate via the helper.
  setupGitCredentialHelper(owner, repo, WORKSPACE);
  sendHeartbeatSync(taskId, `Clone complete, configuring workspace...`);
  git(['config', 'user.email', buildConfig?.git_user_email || 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', buildConfig?.git_user_name || 'Blueprint Agent']);
  // Activate the max-cap union merge driver for module_size_limits.json so a
  // pre-push rebase of competing cap bumps auto-resolves (see .gitattributes).
  registerSizeCapMergeDriver(WORKSPACE);
  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  // Namespace is resolved by the caller (from the raw task) and threaded in, so
  // it survives an empty build_config.directories (resolveBuildConfig → undefined).
  const namespace = taskBranchNamespace;
  const workBranch = `${namespace}/${branchPrefix}-${shortId}`;
  await prepareTaskWorkBranch(
    { owner, repo, ghToken, baseBranch, workBranch, taskId, ws },
    { git, listOpenPullRequestsForBranch, sendLog },
  );
  return { workBranch, shortId };
}

export async function pushAndCreatePR(
  owner: string, repo: string, taskId: string, workBranch: string,
  baseBranch: string, title: string, body: string,
  ws: WebSocket | null, statusRef: { value: string },
  skipRebase = false
): Promise<string> {
  if (!skipRebase) {
    // Rebase onto latest base branch before push to avoid merge conflicts in the PR.
    // If rebase fails, fall back to merge. If merge fails, push as-is — the review
    // pipeline will detect conflicts and spawn a fix task.
    statusRef.value = `Rebasing onto ${baseBranch}...`;
    try {
      git(['fetch', 'origin', baseBranch]);
      sendHeartbeatSync(taskId, `Fetched ${baseBranch}, rebasing...`);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      try { git(['rebase', '--abort']); } catch { /* ignore */ }
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — trying merge instead\n`);
      try {
        git(['merge', `origin/${baseBranch}`, '--no-edit']);
        sendLog(ws, `Merged ${baseBranch} successfully\n`);
      } catch {
        try { git(['merge', '--abort']); } catch { /* ignore */ }
        sendLog(ws, `Merge also conflicted — pushing as-is, review pipeline will handle\n`);
      }
    }

    // Verify no conflict markers before pushing
    try {
      const markers = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: CONFLICT_CHECK_TIMEOUT }).trim();
      if (markers) {
        sendLog(ws, `WARNING: Conflict markers found in:\n${markers}\nAborting rebase and pushing original commit.\n`);
        try { git(['rebase', '--abort']); } catch { /* ignore */ }
      }
    } catch { /* no markers found */ }
  }

  // Use async push so the event loop isn't blocked — the setInterval heartbeat
  // keeps firing during the push, preventing the reaper from killing us mid-push.
  sendHeartbeatSync(taskId, `Pushing ${workBranch}...`);
  const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`) as { data: { token: string } };
  // Refresh the token file and ensure the remote URL is clean (no embedded secret).
  // The credential helper configured in .git/config at clone time will supply the
  // fresh token to git push without it appearing in the URL or .git/config.
  writeGitTokenFile(freshGhRes.data.token);
  const cleanPushUrl = `https://github.com/${owner}/${repo}.git`;
  execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', cleanPushUrl], { encoding: 'utf-8' });
  await gitAsync(['push', 'origin', workBranch]);
  sendLog(ws, `Pushed to branch: ${workBranch}\n`);

  // Heartbeat after push, before PR creation — ensures the reaper sees fresh activity
  sendHeartbeatSync(taskId, `Creating pull request for ${workBranch}...`);
  statusRef.value = 'Creating pull request...';
  let prUrl = '';
  try {
    // Timeout must cover the backend's worst case: get_github_token_for_task,
    // existing-artifact idempotency check (which calls get_pull_request on
    // GitHub), plus create_pull_request itself — each a GitHub call with up
    // to 3 retries and a 120s rate-limit sleep in _github_request. 90s was
    // not enough on rate-limited or slow cycles and every retry hit the same
    // slow path, so tasks failed with AbortError.
    const prRes = await apiPost('/api/agent/github/pull-requests', {
      task_id: taskId, owner, repo, title,
      head: workBranch, base: baseBranch, body,
    }, API_MAX_RETRIES, 240_000) as { data?: { html_url?: string } };
    prUrl = prRes.data?.html_url ?? '';
    sendLog(ws, `PR created: ${prUrl}\n`);
  } catch (e) {
    sendLog(ws, `Failed to create PR: ${String(e)}\n`);
    const err = new Error(`PR creation failed: ${String(e)}`);
    (err as Error & { failureCategory?: FailureCategory }).failureCategory = 'pr_creation_failed';
    throw err;
  }

  return prUrl;
}

// ── Telemetry module ─────────────────────────────────────────────────────────
import {
  errorFingerprint,
  computePromptFingerprint,
  setCurrentPromptFingerprint,
  getCurrentPromptFingerprint,
  recordLlmCallStart,
  recordLlmTokens,
  recordProviderReportedUsage,
  recordNumTurns,
  recordToolResultBytes,
  getLlmTelemetry,
  resetLlmTelemetry,
  recordFormatMiss,
  recordFormatRepairAttempt,
  recordFormatRepairSuccess,
  getFormatComplianceTelemetry,
  resetFormatComplianceTelemetry,
  estimateTokensFromChars,
  recordResidentTurnLatency,
  getResidentTurnLatencyTelemetry,
  resetResidentTurnLatencyTelemetry,
} from './telemetry.js';
export {
  errorFingerprint,
  computePromptFingerprint,
  setCurrentPromptFingerprint,
  getCurrentPromptFingerprint,
  recordLlmCallStart,
  recordLlmTokens,
  recordProviderReportedUsage,
  recordNumTurns,
  recordToolResultBytes,
  getLlmTelemetry,
  resetLlmTelemetry,
  recordFormatMiss,
  recordFormatRepairAttempt,
  recordFormatRepairSuccess,
  getFormatComplianceTelemetry,
  resetFormatComplianceTelemetry,
  estimateTokensFromChars,
  recordResidentTurnLatency,
  getResidentTurnLatencyTelemetry,
  resetResidentTurnLatencyTelemetry,
};

// ── Divergence detection helpers ─────────────────────────────────────────────
export { detectLoopDivergence };


// ── Test runner detection (shared by testFixLoop and captureTestBaseline) ───
export type { TestRunner };
export { detectTestRunners, parseFailingTests, normalizePytestSelectorForRunner, extractPytestSelectorsFromOutput, buildTargetedPytestRunners };

// ── Test verification loop (extracted to utils/test-fix-loop.ts) ─────────────
// Re-exported here so existing importers (and index.test.ts) keep working;
// handlers now import these directly from './utils/test-fix-loop.js'.
export {
  captureTestBaseline, testFixLoop, condensedAnalysisSections, parseFailingTestsFromVitestJson,
} from './utils/test-fix-loop.js';
export type { TestFixLoopOptions } from './utils/test-fix-loop.js';


// ── Claude Code Runner (shared by all handlers) ─────────────────────────────
async function validateProviderAuth(runner: ProviderRunner, reportPreClaimFailure: (reason: string) => void, model?: string): Promise<void> {
  // Bake-time lifecycle smoke: skip all credential checks so the smoke can
  // exercise the full claim→dispatch→complete chain without real LLM creds.
  if (process.env.BLUEPRINT_SMOKE_SKIP_VALIDATION === '1') return;
  // Self-hosted runner: credentials are assigned dynamically by the backend
  // pool via /api/runner/next-task and injected by the runner as env vars.
  // There is no static local env-var credential path — the runner does not
  // carry its own LLM credentials, so the missingCredentials() check that
  // cloud VMs use (boot-materialized env vars) is not consulted here.
  const vmId = process.env.VM_INSTANCE_ID || '';
  const isSelfHostedRunner = vmId.startsWith('runner:');
  if (!isSelfHostedRunner) {
    const missing = runner.missingCredentials();
    if (missing.length > 0) {
      const reason = `missing provider credentials: ${missing.join(', ')}`;
      log(reason);
      reportPreClaimFailure(reason);
      process.exit(1);
    }
  }
  // Boot-materialized config-file presence (poisoned-VM guard, 2026-06-06).
  // On a VM whose boot-materialized ~/.claude.json was destroyed mid-run, the
  // env credentials look perfectly healthy but every Claude invocation dies in
  // seconds with "Claude configuration file not found". Nothing on the VM can
  // recreate the file pre-claim (the boot manifest is not retained on disk),
  // so fail HERE — before any claim — with an explained pre-claim death the
  // orchestrator can tear down, instead of claiming tasks that are doomed.
  // IMPORTANT: this fires ONLY for files that existed at process start (or
  // that the active retarget manifest promised) and are now absent — genuine
  // loss. claude_config_json is an OPTIONAL per-account pool field; accounts
  // without it boot healthy VMs that never have the file (the CLI runs on
  // env/OAuth alone), and refusing those terminally FAILED 49 tasks on
  // 2026-06-06. Gated to real VMs + the expected-set inside
  // missingVmProviderConfigFiles.
  const missingConfig = missingVmProviderConfigFiles(runner.provider);
  if (missingConfig.length > 0) {
    const reason = `vm_config_missing: provider config file(s) present at boot now absent on disk: ${missingConfig.join(', ')} — boot materialization lost; refusing to claim work configless`;
    log(reason);
    reportPreClaimFailure(reason);
    process.exit(1);
  }
  const validation = runner.buildValidationCommand(model);
  if (!validation) return;
  try {
    log(`Validating ${runner.displayName} credentials...`);
    const result = await new Promise<{ exitCode: number; stdout: string; stderr: string }>((resolve) => {
      let stdout = '';
      let stderr = '';
      const check = spawn(validation.command, validation.args, { cwd: '/tmp', env: validation.env });
      const valTimeout = setTimeout(() => { try { check.kill(); } catch { /* ignore */ } }, validation.timeoutMs);
      check.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
      check.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
      check.on('close', (code) => { clearTimeout(valTimeout); resolve({ exitCode: code ?? 1, stdout, stderr }); });
    });
    const combined = `${result.stderr}
${result.stdout}`;
    if (result.exitCode !== 0 && matchesRateLimit(combined, runner.provider, runner.authMode)) {
      const snippet = combined.slice(0, 200);
      log(`${runner.displayName} validation failed — account rate-limited: ${snippet}`);
      reportPreClaimFailure(`token validation rate-limited: ${snippet}`);
      process.exit(1);
    }
    // A validation exit that carries an explicit authentication-rejection signature
    // (HTTP 401 authentication_failed, expired/invalid token, provider-specific
    // invalid-key message) means this VM has no usable credential. Fail BEFORE
    // claiming/running so the task is attributed to a provisioning/auth fault — not
    // silently "continued" into a real task run that dies on the first model call and
    // gets misclassified as a code failure. Note: a bare apiKeySource=none is NOT a
    // rejection (it is normal for the OAuth/Max-plan default), so it is deliberately
    // not matched — see AUTH_FAILURE_PATTERNS_GENERIC in providers/runners.ts.
    if (result.exitCode !== 0 && matchesAuthFailure(combined, runner.provider)) {
      const snippet = combined.slice(0, 200);
      log(`${runner.displayName} validation failed — provider authentication rejected (credentials invalid or unauthorized): ${snippet}`);
      reportPreClaimFailure(`provider auth rejected — credentials invalid or unauthorized: ${snippet}`);
      process.exit(1);
    }
    if (result.exitCode !== 0) {
      log(`${runner.displayName} validation exited non-zero (${result.exitCode}) — continuing`);
      return;
    }
    log(`${runner.displayName} validation OK`);
  } catch (e) {
    log(`${runner.displayName} validation error (continuing): ${e instanceof Error ? e.message : String(e)}`);
  }
}

async function loadVmImageIdentity(): Promise<void> {
  const id = await fetchVmImageIdentity();
  vmImageName = id.image_name;
  vmImageTs = id.image_ts;
  log(`VM image: ${vmImageName ?? '<unknown>'}${vmImageTs ? ` (ts=${vmImageTs})` : ''}`);
}

async function replayStaleResults(): Promise<void> {
  // Retry any stale result files from previous runs (network failure / VM kill mid-POST)
  try {
    const staleFiles = fs.readdirSync('/tmp').filter(f => /^task_.+_result\.json$/.test(f));
    for (const file of staleFiles) {
      const filePath = `/tmp/${file}`;
      let staleEnvelope: PersistedStaleResultEnvelope | null = null;
      try {
        const raw = fs.readFileSync(filePath, 'utf-8');
        staleEnvelope = parsePersistedStaleResultFile(raw);
        const stalePayload = staleEnvelope.payload;
        const staleTaskId = strVal(stalePayload.task_id) || file.replace(/^task_/, '').replace(/_result\.json$/, '');
        const now = Date.now();
        if (shouldAbandonStaleResult(staleEnvelope, now)) {
          log(`Abandoning stale result for task ${staleTaskId} after ${staleEnvelope.retry.attempts} failed retries / ${(now - staleEnvelope.retry.createdAt) / 1000}s age`);
          try { fs.unlinkSync(filePath); } catch { /* ignore */ }
          continue;
        }
        if (staleEnvelope.retry.nextAttemptAt > now) {
          const waitSec = Math.ceil((staleEnvelope.retry.nextAttemptAt - now) / 1000);
          log(`Skipping stale result for task ${staleTaskId}; retry backoff has ${waitSec}s remaining`);
          continue;
        }
        const replayAuth = resolveReplayAgentToken(staleEnvelope, staleTaskId);
        if (!replayAuth.agentToken) {
          log(`Abandoning stale result for task ${staleTaskId}: ${replayAuth.reason}`);
          try { fs.unlinkSync(filePath); } catch { /* ignore */ }
          continue;
        }
        log(`Found stale result file for task ${staleTaskId} — retrying POST`);
        await apiPostWithFixedToken('/api/agent/status', stalePayload, replayAuth.agentToken);
        try { fs.unlinkSync(filePath); } catch { /* ignore */ }
        log(`Stale result for task ${staleTaskId} posted successfully`);
      } catch (e) {
        if (staleEnvelope) {
          const updated = markPersistedStaleResultFailure(staleEnvelope, e);
          const staleTaskId = strVal(updated.payload.task_id) || file.replace(/^task_/, '').replace(/_result\.json$/, '');
          if (shouldAbandonStaleResult(updated)) {
            log(`WARNING: Stale result for task ${staleTaskId} exhausted retries after ${updated.retry.attempts} attempts; deleting file so /tmp cannot grow unbounded`);
            try { fs.unlinkSync(filePath); } catch { /* ignore */ }
          } else {
            writeStaleResultFile(filePath, updated);
            const waitSec = Math.ceil((updated.retry.nextAttemptAt - Date.now()) / 1000);
            log(`WARNING: Failed to retry stale result from ${file}: ${String(e)} — next retry in ${waitSec}s`);
          }
        } else {
          log(`WARNING: Malformed stale result file ${file}: ${String(e)} — deleting`);
          try { fs.unlinkSync(filePath); } catch { /* ignore */ }
        }
      }
    }
  } catch (e) {
    log(`WARNING: Stale result scan failed: ${String(e)}`);
  }
}

async function completeStartup(
  runner: ProviderRunner,
  reportPreClaimFailure: (reason: string) => void,
  bootTaskId: string,
  startupStatusRef?: { value: string },
  authAlreadyValidated = false,
): Promise<void> {
  log(`Provider runtime: provider=${PROVIDER} auth_mode=${AUTH_MODE} execution_mode=${LLM_EXECUTION_MODE}`);
  if (LLM_CLI_TRANSLATION) {
    log(`Settings bag: ${LLM_CLI_TRANSLATION.args.length} CLI args, ${LLM_CLI_TRANSLATION.prompt_fragments.length} prompt fragments`);
    if (LLM_CLI_TRANSLATION.unsupported.length > 0) {
      log(`Settings bag unsupported (not honored by CLI): ${LLM_CLI_TRANSLATION.unsupported.join('; ')}`);
    }
  }
  log(runner.credentialSummary());
  // Provider auth may already have been validated before the first claim (pool
  // mode validates pre-claim so a bad-cred VM fails before flipping a task
  // IN_PROGRESS — matching cold-boot semantics). Re-validating would also reset
  // the OAuth baseline hash AFTER a validation-triggered rotation, masking it.
  if (!authAlreadyValidated) {
    // Snapshot the OAuth auth file *before* provider validation runs. Validation
    // invokes the CLI with the provisioned token, which for Codex may trigger a
    // refresh immediately; without a baseline hash we'd miss rotations that
    // happen during validation itself.
    recordInitialOAuthAuthHash();
    if (startupStatusRef) startupStatusRef.value = 'Completing startup: validating provider auth';
    await validateProviderAuth(runner, reportPreClaimFailure, process.env.LLM_MODEL);
    await persistOAuthRotationAfterProviderValidation(bootTaskId, reportPreClaimFailure);
    if (bootTaskId) {
      sendHeartbeatSync(bootTaskId, 'AGENT_AUTH_OK: provider validation complete');
    }
  }
  if (startupStatusRef) startupStatusRef.value = 'Completing startup: reading runtime fingerprint';
  agentRuntimeFingerprint = readAgentRuntimeFingerprint();
  log(`Agent runtime fingerprint: ${agentRuntimeFingerprint ?? '<not available>'}`);
  agentSourceSha = readAgentSourceSha();
  log(`Agent source git SHA: ${agentSourceSha ?? '<not available>'}`);
  if (startupStatusRef) startupStatusRef.value = 'Completing startup: loading VM image identity';
  await loadVmImageIdentity();
  if (startupStatusRef) startupStatusRef.value = 'Completing startup: replaying stale results';
  await replayStaleResults();
}

// ── Main ────────────────────────────────────────────────────────────────────
export async function main() {
  let _pendingExitCode = 0;
  // Start forensic instrumentation as early as possible. The lag monitor + exit
  // markers don't depend on auth or task claim, so we want them recording from
  // the moment node starts so a pre-claim death still leaves /tmp logs.
  startEventLoopLagMonitor();
  installExitMarkers();

  // Detect Cloud Run Job execution mode from the platform-set CLOUD_RUN_JOB env
  // var (Cloud Run auto-sets it to the job name on job executions; it is reserved
  // and cannot be injected). Presence — not a specific value — is the signal.
  isJobMode = !!process.env.CLOUD_RUN_JOB;

  log(`Blueprint Agent starting (VM: ${VM_INSTANCE_ID})`);
  log(`Registered handlers (${Object.keys(handlers).length}): ${Object.keys(handlers).join(', ')}`);
  log(`Agent source: ${import.meta.url}`);

  // Snapshot which managed provider config files exist on disk at process
  // start, BEFORE provider validation or any claim work can touch them. The
  // pre-claim poisoned-VM guard in validateProviderAuth compares against this
  // baseline so it only refuses on genuine LOSS (file materialized at boot,
  // now gone) and never on never-existed (accounts without the optional
  // claude_config_json boot healthy VMs that never have ~/.claude.json).
  captureBootProviderConfigPresence();

  // When running as a Cloud Run Job the backend does not inject AGENT_SECRET_TOKEN
  // or LLM credential values as plaintext env-var overrides to avoid exposing them
  // in Cloud Audit Logs and run.executions.get. Instead AGENT_SECRET_VERSION is
  // passed and we fetch the manifest here to obtain both the JWT and LLM creds.
  if (isJobMode && !AGENT_TOKEN && process.env['AGENT_SECRET_VERSION']) {
    await resolveCredentialsFromSecretVersion(process.env['AGENT_SECRET_VERSION']);
  }

  if (!AGENT_TOKEN) throw new Error('AGENT_SECRET_TOKEN is required');

  // Publish the live agent-token file location (once) so the provisioned bash
  // helper scripts read the ROTATED token on each invocation instead of the
  // stale spawn-time AGENT_API_TOKEN env snapshot, and mirror the current token
  // there now. Kept fresh on every claim (activateClaimedRole) and refresh
  // (setToken). Fixes the resident-session "writes 401 after ~1h" bug while
  // leaving the 1h security TTL untouched.
  ensureAgentTokenFileEnv();
  writeAgentTokenFile(AGENT_TOKEN);

  // Distinguish a genuine cold boot from a warm re-target re-exec. The
  // re-target path (performWarmRetarget) restarts the systemd unit after
  // resetForNextRole already cleaned + preserved the workspace, stamping
  // BLUEPRINT_REEXEC=1 (+ BLUEPRINT_WARM_HANDOFF=1) into /etc/agent.env. On
  // such a resume we KEEP the warm-handoff flag so prepareWorkspaceForRole
  // reuses the workspace instead of cold-cloning, and we count the role as a
  // warm start so the telemetry the backend stamped (context._timing.warm_start)
  // is not overwritten with a spurious cold sample.
  const isReexecResume = process.env.BLUEPRINT_REEXEC === '1';
  if (isReexecResume) {
    // One-shot marker — consume it so a later genuine cold path is unaffected.
    delete process.env.BLUEPRINT_REEXEC;
  } else {
    // Cold start — ensure prepareWorkspaceForRole takes the fresh-clone path on
    // role 1. The flag is set later by the role-loop AFTER resetForNextRole
    // succeeds; on first boot the workspace is empty and we always cold-clone.
    delete process.env.BLUEPRINT_WARM_HANDOFF;
    // Also clear lane vars so a stale value from a prior process incarnation
    // cannot leak into the first role of a cold boot.
    delete process.env.BLUEPRINT_LANE_MODE;
    delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;
  }

  // Per-role state reset on cold-start. Mirrors the role-loop's warm handoff
  // (same single source of truth) so a stale value from a previous process
  // incarnation (shouldn't happen, but defensive) cannot leak into the first
  // role's status payload. warmStart is true only on a warm re-target re-exec.
  resetPerRoleModuleState({ warmStart: isReexecResume });

  // TASK_ID is baked into /etc/agent.env by the orchestrator's startup script
  // so we can post pre-claim failure heartbeats. The JWT also binds sub=task_id,
  // so the backend heartbeat endpoint accepts this task_id before claim runs.
  const PRE_CLAIM_TASK_ID = process.env.TASK_ID || '';
  const poolMode = process.env.BLUEPRINT_POOL_MODE === '1';
  // Cloud Run Job containers run exactly one task per execution — no warm
  // continuity poll, no heartbeat idle window between roles. Cloud Run auto-sets
  // CLOUD_RUN_JOB (reserved) to the job name, so presence is the signal.
  const jobMode = !!process.env.CLOUD_RUN_JOB;

  // Report a pre-claim failure via the existing heartbeat endpoint. The backend's
  // /api/agent/heartbeat accepts PROVISIONING-status tasks, so this turns silent
  // startup deaths into explained ones. Uses sendHeartbeatSync so the request
  // flushes before the process exits.
  const reportPreClaimFailure = (reason: string) => {
    const reportTaskId = PRE_CLAIM_TASK_ID || process.env.TASK_ID || '';
    const truncated = sanitize(reason).slice(0, 500);
    if (!reportTaskId) {
      // Taskless pool boot: there is no task to attribute the failure to, but a
      // VM-scoped token can carry the reason via /vm-heartbeat so the orchestrator
      // logs an EXPLAINED death instead of reaping an unexplained zombie. Sync
      // curl so the request flushes before the imminent process.exit(1).
      const vmInstanceId = process.env.VM_INSTANCE_ID || '';
      if (poolMode && vmInstanceId) {
        log(`Reporting pre-claim failure via vm-heartbeat for VM ${vmInstanceId}: ${truncated}`);
        try {
          execFileSync('curl', [
            '-sf', '-X', 'POST',
            `${ORCHESTRATOR_URL}/api/agent/vm-heartbeat`,
            '-H', 'Content-Type: application/json',
            '-H', `X-Agent-Token: ${AGENT_TOKEN}`,
            '-d', JSON.stringify({ vm_instance_id: vmInstanceId, pre_claim_failure: truncated }),
          ], { timeout: 10_000, stdio: 'ignore' });
        } catch (e) {
          log(`vm-heartbeat pre-claim failure report failed: ${e instanceof Error ? e.message : String(e)} (non-fatal)`);
        }
        return;
      }
      log(`Cannot report pre-claim failure — TASK_ID env not set. Reason: ${truncated}`);
      return;
    }
    log(`Reporting pre-claim failure for task ${reportTaskId}: ${truncated}`);
    sendHeartbeatSync(reportTaskId, `PRE_CLAIM_FAILED: ${truncated}`);
  };

  // Early "node reached main" beacon. Lands before any async work, so if the
  // agent silently hangs during validation or claim we can at least tell
  // that node started and the TASK_ID env was readable — see the silent
  // pre-claim death forensics in the reaper. Best-effort, non-blocking.
  if (PRE_CLAIM_TASK_ID) {
    sendHeartbeatSync(PRE_CLAIM_TASK_ID, 'AGENT_BOOTED: node reached main(), validating auth');
  }

  // ── Role loop ────────────────────────────────────────────────────────────
  // The first iteration is a cold start — claim via /api/agent/claim and
  // dispatch the handler. Subsequent iterations are warm: pollForNextRole
  // returned a claim, we ran resetForNextRole, and we re-enter the loop
  // body to claim the new task data. The PR_NUMBER threaded through the
  // poll lets the backend scope the lookup so a stray pin doesn't claim
  // the wrong VM.
  let lastTaskId = '';
  let lastPrNumber: number | null = null;
  let lastBaseBranch: string | null = null;

  // Top-level lifecycle catch — runRoleOnce throws on unrecoverable errors
  // (claim failure, OAuth rotation failure, fatal handler exception). The
  // outer catch reports via the existing global crash handlers which fire
  // on the rejected promise from main().
  try {
    if (poolMode) {
      const vmInstanceId = process.env.VM_INSTANCE_ID;
      if (!vmInstanceId) throw new Error('VM_INSTANCE_ID is required in pool mode');
      // Validate provider auth BEFORE claiming the first task, matching cold-boot
      // semantics (the cold path validates in completeStartup before any claim).
      // bootstrapPoolMode flips a task IN_PROGRESS and rotates to a task-scoped
      // token, so a pool VM with bad creds must fail HERE — before claiming —
      // rather than failing a task it already owns. Snapshot the OAuth baseline
      // first so a refresh triggered by validation is detected; completeStartup
      // below is told the auth step is already done (authAlreadyValidated).
      const bootRunner = getProviderRunner();
      recordInitialOAuthAuthHash();
      await validateProviderAuth(bootRunner, reportPreClaimFailure, process.env.LLM_MODEL);
      await persistOAuthRotationAfterProviderValidation('', reportPreClaimFailure);
      captureBootCredentialSnapshot();
      log(`Pool mode enabled — polling for first task (max idle ${getPoolMaxIdleLifetimeMs()}ms)`);
      const poolResult = await bootstrapPoolMode({
        vmInstanceId,
        apiBase: ORCHESTRATOR_URL,
        agentJwt: AGENT_TOKEN,
      });
      if (poolResult.action === 'exit') {
        log(`Pool mode exiting before first claim (reason=${poolResult.reason ?? 'unspecified'})`);
        return;
      }
      log(`Pool mode claimed first task: ${poolResult.taskId ?? ''}`);
      const claimedRunner = getProviderRunner();
      const startupTaskId = process.env.TASK_ID || poolResult.taskId || '';
      const startupStatusRef = { value: 'Completing startup before claim replay' };
      if (startupTaskId) {
        startHeartbeat(startupTaskId, () => startupStatusRef.value);
      }
      try {
        await completeStartup(claimedRunner, reportPreClaimFailure, startupTaskId, startupStatusRef, true);
      } catch (startupErr) {
        stopHeartbeat();
        throw startupErr;
      }
    } else {
      await completeStartup(getProviderRunner(), reportPreClaimFailure, PRE_CLAIM_TASK_ID);
      captureBootCredentialSnapshot();
    }
    let firstRole = true;
    for (;;) {
      let claimResult: RunRoleOnceResult;
      try {
        if (firstRole) {
          firstRole = false;
          // Re-materialize the current account's provider config (incl.
          // ~/.claude.json for anthropic) from the boot snapshot before the FIRST
          // handler runs. A warm/pool VM can lose the boot-materialized config
          // between boot and its first claim; warm roles 2+ already re-materialize
          // in continueWarmContinuation, but the first role relied on the boot file
          // surviving — the gap behind the recurring vm_config_missing warm deaths
          // (e.g. task 7b5c176d / VM cb152cca). Idempotent rewrite on a healthy VM;
          // the self-heal on a poisoned one. If the config genuinely cannot be
          // restored (snapshot itself lacks it) it raises vm_config_missing — report
          // that synchronously so the orchestrator does a clean PROVISION_FAILURE
          // requeue instead of waiting out a vm_unresponsive timeout. A missing boot
          // snapshot is non-fatal here (an account without claude_config_json never
          // promised the file); the existing per-claim guards still apply.
          try {
            if (activeCredentialStateSource === 'retarget') {
              // Pool-mode first claims can already have materialized a
              // backend-supplied account/provider re-target manifest in
              // bootstrapPoolMode. Restoring the frozen boot snapshot here
              // would silently undo that switch immediately before the role
              // runs (for example, launching Claude with an OpenAI model).
              // The retarget path already wrote and validated its credential
              // state; re-check the promised files without replacing it.
              assertVmProviderConfigPresent('first-role');
              log('[first-role] preserving claim-time retarget credential state');
            } else {
              materializeClaimCredentialState(undefined, 'first-role');
            }
          } catch (matErr) {
            const cat = (matErr as { failureCategory?: unknown } | null)?.failureCategory;
            if (cat === 'vm_config_missing') {
              reportPreClaimFailure(
                `vm_config_missing: ${matErr instanceof Error ? matErr.message : String(matErr)}`,
              );
              throw matErr;
            }
            log(`[first-role] credential re-materialize skipped: ${matErr instanceof Error ? matErr.message : String(matErr)}`);
          }
        }
        claimResult = await runRoleOnce({ reportPreClaimFailure });
      } catch (roleErr) {
        // GracefulShutdownSignal is a benign "task went terminal / reassigned"
        // teardown request, NOT a fault. runRoleOnce's finally already stopped
        // the heartbeat and closed the ws; break out so the outer finally
        // tears down the workspace and we exit ONCE (no race with a deferred
        // process.exit, no pollForNextRole re-entry, no double-role-start).
        if (roleErr instanceof GracefulShutdownSignal) {
          log(`Role abandoned gracefully (reason=${roleErr.reason}) — winding down`);
          if (roleErr.exitCode !== 0) _pendingExitCode = roleErr.exitCode;
          break;
        }
        throw roleErr;
      }
      lastTaskId = claimResult.taskId;
      lastPrNumber = claimResult.prNumber;
      lastBaseBranch = claimResult.baseBranch;

      // Cloud Run Job executions run exactly one task — skip warm-continuity
      // poll entirely (no multi-role warm VM; container exits after this role).
      if (jobMode) {
        log('[job-mode] Cloud Run Job — skipping warm-continuity poll, single-role execution complete');
        break;
      }

      // After a successful role completion, poll for a warm next role. The
      // backend decides whether this is an ordinary warm claim or a PARK
      // directive that extends the warm-poll window for a rate-limited task.
      const warmContinuation = await continueWarmContinuation({
        priorTaskId: lastTaskId,
        priorPrNumber: lastPrNumber,
        baseBranch: lastBaseBranch,
        parkDirective: claimResult.parkDirective,
      });
      if (warmContinuation.action === 'exit') break;
    }
  } finally {
    // Workspace teardown happens once at process exit, not per-role —
    // warm continuity DEPENDS on the workspace surviving across roles.
    try { execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8', timeout: 30_000 }); } catch { /* ignore */ }
    log('Agent shutdown complete.');
  }
  if (_pendingExitCode !== 0) process.exit(_pendingExitCode);
}

// ── Per-role lifecycle (extracted so warm roles re-enter cleanly) ────────────
//
// Encapsulates one role end-to-end: claim → setup heartbeat + WS → fetch GH
// creds → dispatch handler → success/failure status post → teardown
// heartbeat + WS + per-role /tmp scratch. `main()` calls this once for the
// initial cold role and again for each warm role discovered via
// pollForNextRole. Returns the claimed task's id + PR number so the role
// loop can thread them into the next /api/agent/next-role call.

interface RunRoleOnceOptions {
  reportPreClaimFailure: (reason: string) => void;
}

interface RunRoleOnceResult {
  taskId: string;
  prNumber: number | null;
  parkDirective: ParkDirective | null;
  /**
   * The base branch of the just-completed task. Threaded through to the
   * role-loop so resetForNextRole's optional fetch+rebase step (step 5) can
   * actually run. Without this, baseBranch was always undefined and step 5
   * was dead code in production.
   */
  baseBranch: string | null;
}

interface WarmContinuationDeps {
  pollForNextRole: typeof pollForNextRole;
  resetForNextRole: typeof resetForNextRole;
  activateClaimedRole: typeof activateClaimedRole;
  materializeClaimCredentialState: typeof materializeClaimCredentialState;
  performWarmRetarget: typeof performWarmRetarget;
  resolveVmInstanceId: (allowJwtFallback: boolean) => string | null;
}

const warmContinuationDeps: WarmContinuationDeps = {
  pollForNextRole,
  resetForNextRole,
  activateClaimedRole,
  materializeClaimCredentialState,
  performWarmRetarget,
  resolveVmInstanceId: (allowJwtFallback: boolean) => {
    const vmInstanceId = process.env.VM_INSTANCE_ID;
    if (vmInstanceId) return vmInstanceId;
    return allowJwtFallback ? decodeJwtVmInstanceId(AGENT_TOKEN) : null;
  },
};

function parkPollTimeoutMs(parkDirective: ParkDirective): number {
  const deadlineMs = Date.parse(parkDirective.parkDeadline);
  return Number.isFinite(deadlineMs) ? Math.max(0, deadlineMs - Date.now()) : 0;
}

export async function continueWarmContinuation(
  opts: {
    priorTaskId: string;
    priorPrNumber: number | null;
    baseBranch: string | null;
    parkDirective?: ParkDirective | null;
  },
  deps: WarmContinuationDeps = warmContinuationDeps,
): Promise<{ action: 'continue' | 'exit' }> {
  const vmInstanceId = deps.resolveVmInstanceId(!!opts.parkDirective);
  if (!vmInstanceId) {
    return { action: 'exit' };
  }

  // Clear TASK_ID for the poll window so a benign SIGTERM during park does
  // not overwrite the prior task with a false shutdown report.
  process.env.TASK_ID = '';
  _inWarmPollWindow = true;

  let pollResult: PollResult;
  try {
    pollResult = await deps.pollForNextRole({
      vmInstanceId,
      priorTaskId: opts.priorTaskId,
      priorPrNumber: opts.priorPrNumber,
      apiBase: ORCHESTRATOR_URL,
      agentJwt: AGENT_TOKEN,
      ...(opts.parkDirective ? { timeoutMs: parkPollTimeoutMs(opts.parkDirective) } : {}),
    });
  } catch (pollErr) {
    // Defensive — pollForNextRole has its own internal try/catch, but if a
    // programmer error escapes it we don't want to crash the VM. Treat as exit
    // and let the orchestrator cold-provision the next one.
    log(`pollForNextRole threw: ${String(pollErr)} — exiting`);
    return { action: 'exit' };
  }

  if (pollResult.action === 'exit') {
    log(`pollForNextRole returned exit (reason=${pollResult.reason ?? 'unspecified'})`);
    return { action: 'exit' };
  }

  // claim — rotate the agent token, reset, then re-enter the loop.
  log(`Warm-claimed next role: task ${pollResult.taskId ?? ''}`);
  deps.activateClaimedRole(pollResult, 'reset_failed: warm-claim');
  try {
    await deps.resetForNextRole({
      workspace: WORKSPACE,
      priorTaskId: opts.priorTaskId,
      baseBranch: opts.baseBranch ?? undefined,
    });
  } catch (resetErr) {
    const resetMsg = resetErr instanceof Error ? resetErr.message : String(resetErr);
    log(`resetForNextRole failed: ${resetMsg}`);
    const err = new Error(`reset_failed: ${resetMsg}`) as Error & { failureCategory?: FailureCategory };
    err.failureCategory = 'reset_failed';
    throw err;
  }
  deps.materializeClaimCredentialState(pollResult.secretManifest, 'reset_failed: warm-claim');
  // Lane workspace-mode: set env vars BEFORE BLUEPRINT_WARM_HANDOFF so
  // prepareWorkspaceForRole reads a consistent lane state. Clear them on
  // non-lane claims to prevent leakage across roles on the same warm VM.
  if (pollResult.isLanePhase && pollResult.laneCheckpointSha) {
    process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = pollResult.laneCheckpointSha;
    process.env.BLUEPRINT_LANE_MODE = '1';
  } else {
    delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;
    delete process.env.BLUEPRINT_LANE_MODE;
  }
  process.env.BLUEPRINT_WARM_HANDOFF = '1';
  resetPerRoleModuleState({ warmStart: true });

  const credentialRefreshOnly =
    pollResult.secretManifest?.metadata?.credential_refresh_only === true;
  if (pollResult.secretManifest && !credentialRefreshOnly) {
    log('Warm claim is account-divergent — re-materializing provider creds + re-exec');
    try {
      await deps.performWarmRetarget(pollResult.secretManifest, {
        rotatedJwt: AGENT_TOKEN,
        taskId: pollResult.taskId ?? '',
      });
    } catch (rtErr) {
      const rtMsg = rtErr instanceof Error ? rtErr.message : String(rtErr);
      log(`warm re-target failed: ${rtMsg}`);
      const err = new Error(`reset_failed: warm re-target failed: ${rtMsg}`) as Error & { failureCategory?: FailureCategory };
      err.failureCategory = 'reset_failed';
      throw err;
    }
    return { action: 'exit' };
  }
  if (credentialRefreshOnly) {
    log('Warm claim refreshed same-account rotating provider credentials in place');
  }

  return { action: 'continue' };
}

async function runRoleOnce(opts: RunRoleOnceOptions): Promise<RunRoleOnceResult> {
  const { reportPreClaimFailure } = opts;
  const PRE_CLAIM_TASK_ID = process.env.TASK_ID || '';

  if (PRE_CLAIM_TASK_ID) {
    sendHeartbeatSync(PRE_CLAIM_TASK_ID, 'AGENT_CLAIMING: requesting task payload');
  }
  let task: Record<string, unknown>;
  try {
    task = await apiPost('/api/agent/claim') as Record<string, unknown>;
  } catch (claimErr) {
    const errMsg = claimErr instanceof Error ? `${claimErr.message}${claimErr.stack ? '\n' + claimErr.stack : ''}` : String(claimErr);
    reportPreClaimFailure(`claim failed: ${errMsg}`);
    throw claimErr;
  }
  // Phase 0 telemetry: tClaim = now. firstToolUseAt is filled in by the
  // first recordToolUse() call after this point. setup_secs = the gap.
  roleClaimTimestamp = Date.now();
  const taskId = strVal(task.task_id);
  process.env.TASK_ID = taskId;  // Expose for crash handlers (they can't access local vars)
  setCrashReportTaskId(taskId);  // Decoupled crash identity — survives the poll-window TASK_ID clear
  _inWarmPollWindow = false;     // We're back in active role execution
  const payloads = Array.isArray(task.payloads) ? task.payloads as Record<string, unknown>[] : [];
  const payload: Record<string, unknown> = payloads[0] ?? {};
  const taskType = strVal(payload.type, 'create');
  const model = payload.model ? strVal(payload.model) : (process.env.LLM_MODEL || undefined);
  const effort = payload.effort ? strVal(payload.effort) : undefined;
  const projectMetadata = task.project_metadata as Record<string, unknown> | undefined;
  const buildConfig = (projectMetadata?.build_config ?? {}) as Record<string, unknown>;
  // Attach buildConfig to the task object so handlers can access it via task.build_config
  task.build_config = buildConfig;
  log(`Claimed task ${taskId} (type: ${taskType}): "${strVal(task.title)}"`);

  // Capture pr_number for the warm-VM next-role poll. Different handlers
  // store it under different keys (review/fix use payload.pr_number,
  // research-plan derives it later); we read whatever is present at claim
  // time and accept null otherwise.
  let prNumber: number | null = null;
  const rawPr = payload.pr_number;
  if (typeof rawPr === 'number' && Number.isFinite(rawPr)) {
    prNumber = rawPr;
  } else if (typeof rawPr === 'string' && rawPr.trim() && Number.isFinite(Number(rawPr))) {
    prNumber = Number(rawPr);
  }

  // Capture the task's base_branch so the role-loop can pass it to
  // resetForNextRole(...) and the optional fetch+rebase step actually runs
  // between warm roles. Routes through resolveBaseBranch so projects whose
  // integration branch is not 'main' do not silently fall through to 'main'.
  const baseBranchForReset = resolveBaseBranch(payload, task).trim();

  // Start heartbeat & log stream
  const statusRef: { value: string; toolStartedAt?: number; lastProgressAt?: number } = { value: 'Initializing...' };
  // Pool mode may keep a startup heartbeat alive through completeStartup() and
  // the idempotent claim replay. Replace that timer with the normal per-role
  // heartbeat before the handler begins so only one task heartbeat remains.
  stopHeartbeat();
  startHeartbeat(taskId, () => buildHeartbeatStatus(statusRef, Date.now()));
  scheduleProactiveTokenRefresh(AGENT_TOKEN);
  const ws = await connectLogStream(taskId);
  if (!ws) {
    log('WARNING: WebSocket log stream unavailable — logs will be sent via status updates only');
  }
  sendLog(ws, `Agent started (VM: ${VM_INSTANCE_ID}, type: ${taskType})\n`);

  try {
    // Get GitHub credentials
    // Timeout must cover the backend's worst case for get_github_token_for_task:
    // _github_request sleeps up to 120s on a near-empty rate-limit bucket, plus up
    // to 3 retries each with a 15s httpx timeout. 180s comfortably covers the full
    // backoff. The same path is already covered at 240s for the PR-create call
    // (line ~2724); the token-only fetch here uses 180s (no extra GitHub round-trips
    // beyond token minting). Any failure here is infrastructure, not a code fault.
    statusRef.value = 'Fetching GitHub credentials...';
    let ghRes: { data: { token: string; owner: string; repo: string } };
    try {
      ghRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`, API_MAX_RETRIES, 180_000) as { data: { token: string; owner: string; repo: string } };
    } catch (e) {
      const err = e instanceof Error ? e : new Error(String(e));
      (err as Error & { failureCategory?: FailureCategory }).failureCategory = 'github_token_fetch_failed';
      throw err;
    }
    const { token: ghToken, owner, repo } = ghRes.data;
    log(`GitHub: ${owner}/${repo}`);

    // Load the published task-capability contract before any handler runs. The
    // agent token is valid here (the github/token fetch above just succeeded
    // with it), and the guard makes this a once-per-VM cost. configureAgentPermissions
    // / isReadOnlyTaskType throw until this completes, so a missing or incomplete
    // contract fails the role loudly here rather than silently mis-granting write
    // permissions to a read-only task.
    statusRef.value = 'Loading task-capability contract...';
    await ensureTaskCapabilityContractLoaded();

    // Seed the GitHub token file immediately so bash helpers can use it from
    // the first invocation. Then schedule proactive refreshes every 45 min
    // (the GitHub installation token TTL is 60 min; the backend cache is 50 min,
    // so force_refresh=false hits the cache at zero GitHub API cost).
    ensureGithubTokenFileEnv();
    writeGithubTokenFile(ghToken);
    scheduleGithubTokenRefresh(taskId, WORKSPACE, owner, repo, isReadOnlyTaskType(taskType));

    // Dispatch to handler — contract-driven (registry-driven task types).
    //
    // A BESPOKE handler (exact-type match in the handlers map) preserves all
    // current behavior. Otherwise we resolve a GENERIC runner from the
    // backend-published `task_capability` contract (run_shape × agent_mode ×
    // output_kind), so a task type the backend REGISTERS becomes runnable
    // WITHOUT an agent code edit + redeploy.
    //
    // Audit 2026-04-25 (W4/C4): we still fail fast on a genuinely unrunnable
    // type — a run-shape with no generic runner yet (the reserved
    // experiment_loop, or any unrecognized shape) throws with a clear message
    // rather than silently doing wrong work. But a NEW type of a known shape
    // (single_agent read-only knowledge, or workflow write/PR) no longer throws.
    const resolution = resolveHandler(taskType, task, payload);
    if (!resolution.handler) {
      throw new Error(resolution.reason);
    }
    if (resolution.dispatch === 'generic') {
      log(`No bespoke handler for "${taskType}" — dispatching via contract-driven generic runner (${resolution.runnerLabel}).`);
      sendLog(ws, `Dispatching task type "${taskType}" via generic ${resolution.runnerLabel} runner (contract-driven).\n`);
    }
    const handler = resolution.handler;
    await handler(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
    const parkedAfterSuccess = consumePendingParkDirective(taskId);
    if (parkedAfterSuccess) {
      return {
        taskId,
        prNumber,
        baseBranch: baseBranchForReset,
        parkDirective: parkedAfterSuccess,
      };
    }
    // Phase 0 telemetry: t_handler_complete. Captured only on the success
    // path so failures don't pollute the completion-duration histogram.
    // enrichStatusPayload picks this up on the very next status post and
    // resets between roles in the role-loop's per-role telemetry block.
    // roleClaimTimestamp was set unconditionally above (line ~3197); the
    // Math.max guards against any clock skew producing a negative delta.
    const handlerDurationSecs = (Date.now() - roleClaimTimestamp) / 1000;
    handlerDurationSecsForPayload = Math.max(0, handlerDurationSecs);
    sendLog(ws, '\nTask completed.\n');

  } catch (error) {
    // Benign cancellation during startup: the task went terminal
    // (CANCELLED/COMPLETED/superseded) between /api/agent/claim and the
    // /api/agent/github/token fetch above, so verify_agent_token 401s with
    // "Task is no longer active". This is NOT a code fault — do not record it
    // as failure_category 'fatal' or post a FAILED status (the task is already
    // terminal and the backend would reject the status anyway). Mirror the
    // heartbeat reassignment path (~line 1012): stop the heartbeat, allow a
    // brief wind-down, and exit 0 so the role loop winds down cleanly instead
    // of crash-reporting a fatal failure.
    if (isTaskNoLongerActiveError(error)) {
      log('Task no longer active (cancelled/superseded during startup) — abandoning role gracefully');
      sendLog(ws, '\nTask no longer active (cancelled/superseded during startup) — abandoning role gracefully.\n');
      statusRef.value = 'Task no longer active — abandoning role gracefully';
      // Throw a typed shutdown signal rather than `setTimeout(exit); return`.
      // The old pattern returned successfully back into main()'s role loop,
      // which then proceeded to pollForNextRole — racing the deferred 2s
      // exit and risking a double-role-start (and leaking the in-flight
      // teardown). The throw unwinds through this function's `finally`
      // (which stops the heartbeat + closes the ws) and is caught in main()'s
      // role loop, which breaks cleanly and exits once. requestCancel() stops
      // any heartbeat tick from re-tripping its own self-terminate path.
      requestCancel();
      // Intentional non-Error control-flow throw: GracefulShutdownSignal must
      // NOT be an Error so the fault-classification paths (runRoleOnce catch,
      // global crash handler) never mis-record this benign teardown as a fault.
      // eslint-disable-next-line @typescript-eslint/only-throw-error -- typed graceful-shutdown control signal, caught by main()'s role loop
      throw new GracefulShutdownSignal('task_no_longer_active');
    }
    const errMsg = error instanceof Error ? error.message : String(error);
    const safeErr = sanitize(errMsg);
    log(`Fatal error: ${safeErr}`);
    sendLog(ws, `\nFatal error: ${safeErr}\n`);
    // Classify the failure — rate limit / usage exhaustion should NOT burn retry budget.
    // Detection is carried via RateLimitError (thrown from runClaudeCode). An
    // earlier version re-ran matchesRateLimit on the outer error message, but
    // that message was agent-invented wording ("Claude API usage exhausted —")
    // which doesn't contain any RATE_LIMIT_PATTERNS substring, silently
    // reclassifying rate-limits as CODE_ERROR.
    const isRateLimit = error instanceof RateLimitError;
    const typedError = error instanceof Error
      ? (error as Error & { failureCategory?: FailureCategory })
      : null;
    const errorCategory: FailureCategory | undefined = !isRateLimit ? typedError?.failureCategory : undefined;
    const failureCategory: FailureCategory = isRateLimit ? 'rate_limited' : (errorCategory ?? 'fatal');
    // Surface the error through the heartbeat so it's visible even if the
    // status POST fails — the heartbeat updates latest_status_message on the
    // backend, giving us forensic evidence instead of silent death.
    statusRef.value = `[ERROR] ${safeErr.slice(0, 200)}`;
    // Retry status POST aggressively — if this fails, the backend never learns
    // the result and the reaper eventually times out with no forensic evidence.
    // Keep heartbeat alive during retries so the reaper doesn't kill us mid-report.
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await persistAndPostStatus(taskId, {
          task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
          logs: sanitize(`Fatal error: ${errMsg}`),
          failure_category: failureCategory,
        });
        break; // Success — stop retrying
      } catch (e) {
        const postErr = e instanceof Error ? e.message : String(e);
        log(`Failed to report error (attempt ${attempt + 1}/3): ${postErr}`);
        statusRef.value = `[ERROR] status POST failed: ${sanitize(postErr).slice(0, 150)}`;
        if (attempt < 2) await new Promise(r => setTimeout(r, 10_000 * (attempt + 1)));
      }
    }
    const parkedAfterFailure = consumePendingParkDirective(taskId);
    if (parkedAfterFailure) {
      return {
        taskId,
        prNumber,
        baseBranch: baseBranchForReset,
        parkDirective: parkedAfterFailure,
      };
    }
    // runtime_unavailable is an infra decline (missing CLI binary), not a real
    // task failure. Convert to GracefulShutdownSignal so main() can exit with
    // code 42, signalling the runner to skip the failure-streak increment.
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- GracefulShutdownSignal is a typed control signal, not an Error
    if (error instanceof RuntimeUnavailableError) throw new GracefulShutdownSignal('runtime_unavailable', 42);
    // Re-throw so the role loop terminates — failures don't get warm
    // continuity; the orchestrator cold-provisions for the next role.
    throw error;
  } finally {
    stopHeartbeat();
    if (ws) ws.close();
    // Per-role /tmp scratch cleanup. Workspace teardown happens at process
    // exit (in main()'s outer finally) so warm continuity preserves it.
    try { fs.unlinkSync(`/tmp/agent_prompt_${taskId}.txt`); } catch { /* ignore */ }
    for (const name of TMP_OUTPUT_NAMES) {
      try { fs.unlinkSync(`/tmp/${name}_${taskId}.json`); } catch { /* ignore */ }
    }
  }

  return { taskId, prNumber, baseBranch: baseBranchForReset, parkDirective: null };
}

// Global crash handlers — report errors via heartbeat before dying.
// process.exit() skips the finally block, so the crash handler is the
// ONLY chance to surface forensic evidence to the backend.  Without this,
// every failure mode (uncaughtException, SIGTERM, cgroup OOM SIGKILL)
// looks identical: heartbeats stop, task stays IN_PROGRESS, no evidence.
function _crashReport(label: string, detail: string, exitCode: number) {
  const safeDetail = sanitize(detail);
  console.error(`[FATAL] ${label}: ${safeDetail}`);
  // Local file marker before any network attempt — survives even if the
  // sendHeartbeatSync curl times out or the network is partitioned. Combined
  // with serial-console capture (plan task 25075953), we get the agent's
  // last words even when the heartbeat never lands.
  recordFinalState(`crash:${label}`, safeDetail);
  // Synchronous curl — fire-and-forget fetch() couldn't guarantee the write
  // flushed before process.exit, so crashes would show up as silent heartbeat
  // timeouts instead of explained failures. 3s timeout in the signal-handler
  // path: GCE gives ~30s before SIGKILL, and a 10s blocking curl chews 1/3 of
  // that window for one best-effort flush. 3s fails fast on partition while
  // still succeeding on healthy networks.
  // Prefer the env TASK_ID (active role); fall back to the decoupled
  // _crashReportTaskId so a crash during the warm-poll window — where the role
  // loop deliberately blanks process.env.TASK_ID — still carries task context
  // instead of being silently dropped (cluster C7). A benign SIGTERM during
  // the poll gap is intentionally NOT reported against a task (it's just the VM
  // being torn down with no follow-up work), so it keeps the empty-env path;
  // only genuine crashes (uncaughtException / unhandledRejection) fall back to
  // _crashReportTaskId, and they're labelled distinctly so the backend can tell
  // a real poll-gap crash from the prior task's completed-and-EVALUATING work.
  const envTaskId = process.env.TASK_ID;
  const pollGapCrash = !envTaskId && _inWarmPollWindow && label !== 'SIGTERM' && !!_crashReportTaskId;
  const taskId = envTaskId || (pollGapCrash ? _crashReportTaskId : '');
  const reportLabel = pollGapCrash ? `${label}:during-poll` : label;
  if (taskId) {
    try {
      const metrics = collectMetrics();
      const msg = `[FATAL:${reportLabel}] ${safeDetail.slice(0, 300)}`;
      sendHeartbeatSync(taskId, `[${metrics.heap_used_mb}MB cg:${metrics.cgroup_mb || '?'}MB] ${msg}`, undefined, 3_000);
    } catch { /* best effort — we're dying anyway */ }
  }
  // process.exit normally never returns; if it throws (e.g. when intercepted
  // by a test harness like vitest), swallow the throw so the caller — often
  // an uncaughtException handler — doesn't re-throw and crash the process
  // with a confusing secondary error instead of the original one.
  try { process.exit(exitCode); } catch { /* intercepted, best effort */ }
}

// Only register global handlers and call main() when this file is the entry
// point. When imported by tests (vitest), process.argv[1] points to vitest's
// worker, not this file, so these side-effects are skipped and the worker
// doesn't crash from the still-registered handlers calling process.exit.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.on('unhandledRejection', (reason) => {
    _crashReport('unhandledRejection', String(reason), 1);
  });
  process.on('uncaughtException', (err) => {
    _crashReport('uncaughtException', err instanceof Error ? `${err.message}\n${err.stack}` : String(err), 1);
  });
  process.on('SIGTERM', () => {
    _crashReport('SIGTERM', 'VM shutting down', 143);
  });

  main().catch((err: unknown) => {
    // Route through _crashReport so rejections before startHeartbeat() reaches
    // its first tick still produce a FATAL heartbeat the orchestrator can see.
    // Without this, the catch handler swallows the rejection and no global
    // handler fires — the task stays IN_PROGRESS until the reaper gives up.
    try {
      const detail = err instanceof Error ? `${err.message}\n${err.stack ?? ''}` : String(err);
      _crashReport('main_failed', detail, 1);
    } catch (reportErr) {
      console.error('Fatal error (crash report also failed):', err, reportErr);
      process.exit(1);
    }
  });
}
