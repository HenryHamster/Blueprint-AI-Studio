// ── BuildConfig resolution ──────────────────────────────────────────────────
// Extracts and type-narrows build_config from a task object, returning
// the BuildConfig type defined in index.ts (directories, git identity, etc.).
// Populated from task.build_config (a JSONB field on Project passed via claim).

import type { BuildConfig, DirectoryConfig, MigrationConfig } from './index.js';
import { SHELL_CMD_RE } from './shell-safety.js';

const VALID_TYPES = new Set(['python', 'node', 'go', 'rust', 'other']);
const PROJECT_VERIFICATION_HARNESS_RE = /^[A-Za-z0-9_./-]+$/;

function normalizeBuildConfigDirectoryPath(path: string): string {
  let normalized = path.trim();
  normalized = normalized.replace(/\/+$/, '');
  return normalized || '.';
}

/**
 * Extract and type-narrow build_config from a task object.
 *
 * Returns undefined (triggers hardcoded fallback) when:
 * - task.project_metadata?.skip_build_config is true (project opted into hardcoded fallback)
 * - directories array is missing or empty
 */
export function resolveBuildConfig(task: Record<string, unknown>): BuildConfig | undefined {
  const projectMetadata = task.project_metadata as Record<string, unknown> | undefined;
  if (projectMetadata?.skip_build_config === true) return undefined;

  const raw = task.build_config as Record<string, unknown> | undefined;
  if (!raw) return undefined;

  const rawDirs = raw.directories;
  if (!Array.isArray(rawDirs) || rawDirs.length === 0) return undefined;

  const directories: DirectoryConfig[] = [];
  for (const d of rawDirs) {
    if (!d || typeof d !== 'object') continue;
    const dir = d as Record<string, unknown>;
    const rawPath = typeof dir.path === 'string' ? dir.path : '';
    if (!rawPath) continue;
    const path = normalizeBuildConfigDirectoryPath(rawPath);
    // Reject path traversal: no .., no absolute paths, no null bytes
    if (path.includes('..') || path.startsWith('/') || path.includes('\0')) continue;
    if (!SHELL_CMD_RE.test(path)) continue;
    // Accept 'type' directly, or fall back to 'language' for backwards compat
    const rawType = dir.type ?? dir.language;
    const type = VALID_TYPES.has(rawType as string) ? (rawType as string) : 'python';
    const entry: DirectoryConfig = { path, type };
    // Support both old (lint_command/build_command/test_command) and new (lint_cmd/build_cmd/test_cmd) field names
    const lintCmd = dir.lint_cmd ?? dir.lint_command;
    const buildCmd = dir.build_cmd ?? dir.build_command;
    const testCmd = dir.test_cmd ?? dir.test_command;
    const installCmd = dir.install_cmd;
    // Retain command strings VERBATIM — including quotes and shell operators
    // (`pytest -k "not slow"`, `go test ./... && go vet ./...`). These are NOT
    // dropped on a SHELL_CMD_RE miss: the backend's tests_gated decision counts
    // any non-empty test_cmd as a gating suite, so dropping a metachar command
    // here would make the agent report 'no tests detected' while the backend
    // still believes the project is test-gated — the exact divergence that
    // merges UNVERIFIED. The command sinks now handle these safely: the no-CI
    // gate runs them via a `bash -c` argv (execFile, no interpolation — see
    // detectTestRunners), and the best-effort ./check generator skips any
    // command unsafe to single-quote-interpolate. The `path` traversal +
    // SHELL_CMD_RE check above is retained because path IS interpolated as a cwd
    // with no skip fallback.
    if (typeof lintCmd === 'string') entry.lint_cmd = lintCmd;
    if (typeof buildCmd === 'string') entry.build_cmd = buildCmd;
    if (typeof testCmd === 'string') entry.test_cmd = testCmd;
    if (typeof installCmd === 'string') entry.install_cmd = installCmd;
    const confirmedTestCmd = dir.confirmed_test_cmd;
    const confirmedBuildCmd = dir.confirmed_build_cmd;
    const confirmedInstallCmd = dir.confirmed_install_cmd;
    const confirmedLintCmd = dir.confirmed_lint_cmd;
    if (typeof confirmedTestCmd === 'string') entry.confirmed_test_cmd = confirmedTestCmd;
    if (typeof confirmedBuildCmd === 'string') entry.confirmed_build_cmd = confirmedBuildCmd;
    if (typeof confirmedInstallCmd === 'string') entry.confirmed_install_cmd = confirmedInstallCmd;
    if (typeof confirmedLintCmd === 'string') entry.confirmed_lint_cmd = confirmedLintCmd;
    if (dir.commands_stale === true) entry.commands_stale = true;
    const projectVerification = dir.project_verification;
    if (projectVerification && typeof projectVerification === 'object') {
      const verification = projectVerification as Record<string, unknown>;
      if (
        verification.protocol === 'blueprint-v1'
        && typeof verification.harness === 'string'
        && verification.harness.length > 0
        && !verification.harness.startsWith('/')
        && !verification.harness.includes('..')
        && !verification.harness.includes('\0')
        && PROJECT_VERIFICATION_HARNESS_RE.test(verification.harness)
      ) {
        entry.project_verification = {
          protocol: 'blueprint-v1',
          harness: verification.harness,
        };
      }
    }
    directories.push(entry);
  }

  if (directories.length === 0) return undefined;

  const config: BuildConfig = { directories };

  if (typeof raw.language === 'string') config.language = raw.language;
  if (raw.env_vars && typeof raw.env_vars === 'object' && !Array.isArray(raw.env_vars)) {
    config.env_vars = raw.env_vars as Record<string, string>;
  }
  // Support both nested git_identity and flat git_user_* fields
  if (raw.git_identity && typeof raw.git_identity === 'object') {
    const gi = raw.git_identity as Record<string, unknown>;
    if (typeof gi.name === 'string') config.git_user_name = gi.name;
    if (typeof gi.email === 'string') config.git_user_email = gi.email;
  } else {
    if (typeof raw.git_user_name === 'string') config.git_user_name = raw.git_user_name;
    if (typeof raw.git_user_email === 'string') config.git_user_email = raw.git_user_email;
  }

  // Staging-branch prefixes (research/plan rollup heads). Apply the same
  // path-traversal + shell-safety hygiene as directory paths since these are
  // interpolated into reviewer instructions; drop malformed entries. If every
  // entry is rejected (or the field is the wrong shape) leave it unset so
  // callers fall back to the Blueprint default (["research/", "plan/"]).
  if (Array.isArray(raw.staging_branch_prefixes)) {
    const prefixes: string[] = [];
    for (const p of raw.staging_branch_prefixes) {
      if (typeof p !== 'string' || p === '') continue;
      if (p.includes('..') || p.startsWith('/') || p.includes('\0')) continue;
      if (!SHELL_CMD_RE.test(p)) continue;
      prefixes.push(p);
    }
    if (prefixes.length > 0) config.staging_branch_prefixes = prefixes;
  }

  // Per-project feature-branch namespace. The orchestrator operates on the
  // branch it derives as `${task_branch_namespace}/<type>-<id>`; if the agent
  // drops this field it falls back to the "blueprint" default in
  // setupWorkspace, so every branch diverges from what the orchestrator looks
  // for — orphaning the PR on any non-Blueprint repo (the namespace-divergence
  // bug). Accept only a single branch-safe slug ([a-z0-9-], no '/'), matching
  // the backend's _derive_task_branch_namespace slugifier.
  if (
    typeof raw.task_branch_namespace === 'string' &&
    /^[a-z0-9][a-z0-9-]{0,39}$/.test(raw.task_branch_namespace)
  ) {
    config.task_branch_namespace = raw.task_branch_namespace;
  }

  // Project-specific migration layout (alembic versions dir, models dir,
  // schema-revision file/constant). Each entry is path/identifier-shaped and
  // gets interpolated into the migrate prompt, so apply the same
  // path-traversal + shell-safety hygiene as directory paths; drop malformed
  // values so a bad config falls back to the Blueprint default rather than
  // injecting garbage. An empty schema_revision_file is preserved (it is the
  // explicit "this repo has no schema-revision gate" signal).
  if (raw.migration && typeof raw.migration === 'object' && !Array.isArray(raw.migration)) {
    const m = raw.migration as Record<string, unknown>;
    const migration: MigrationConfig = {};
    const pathField = (v: unknown): string | undefined => {
      if (typeof v !== 'string' || v === '') return undefined;
      if (v.includes('..') || v.startsWith('/') || v.includes('\0')) return undefined;
      if (!SHELL_CMD_RE.test(v)) return undefined;
      return v.replace(/\/$/, '');
    };
    const versionsDir = pathField(m.versions_dir);
    const modelsDir = pathField(m.models_dir);
    const alembicCwd = pathField(m.alembic_cwd);
    if (versionsDir !== undefined) migration.versions_dir = versionsDir;
    if (modelsDir !== undefined) migration.models_dir = modelsDir;
    if (alembicCwd !== undefined) migration.alembic_cwd = alembicCwd;
    // schema_revision_file: '' is meaningful (no gate) but must still be safe.
    if (typeof m.schema_revision_file === 'string') {
      if (m.schema_revision_file === '') {
        migration.schema_revision_file = '';
      } else {
        const sf = pathField(m.schema_revision_file);
        if (sf !== undefined) migration.schema_revision_file = sf;
      }
    }
    if (typeof m.schema_revision_const === 'string' && m.schema_revision_const !== '' && SHELL_CMD_RE.test(m.schema_revision_const)) {
      migration.schema_revision_const = m.schema_revision_const;
    }
    if (Object.keys(migration).length > 0) config.migration = migration;
  }

  return config;
}
