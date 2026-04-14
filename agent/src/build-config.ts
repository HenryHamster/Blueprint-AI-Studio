// ── BuildConfig resolution ──────────────────────────────────────────────────
// Extracts and type-narrows build_config from a task object, returning
// the BuildConfig type defined in index.ts (directories, git identity, etc.).
// Populated from task.build_config (a JSONB field on Project passed via claim).

import type { BuildConfig, DirectoryConfig } from './index.js';

const VALID_TYPES = new Set(['python', 'node', 'go', 'rust']);

/**
 * Extract and type-narrow build_config from a task object.
 *
 * Returns undefined (triggers hardcoded fallback) when:
 * - task.project_metadata?.is_self_repo is true (Blueprint's own repo)
 * - directories array is missing or empty
 */
export function resolveBuildConfig(task: Record<string, unknown>): BuildConfig | undefined {
  const projectMetadata = task.project_metadata as Record<string, unknown> | undefined;
  if (projectMetadata?.is_self_repo === true) return undefined;

  const raw = task.build_config as Record<string, unknown> | undefined;
  if (!raw) return undefined;

  const rawDirs = raw.directories;
  if (!Array.isArray(rawDirs) || rawDirs.length === 0) return undefined;

  const directories: DirectoryConfig[] = [];
  for (const d of rawDirs) {
    if (!d || typeof d !== 'object') continue;
    const dir = d as Record<string, unknown>;
    const path = typeof dir.path === 'string' ? dir.path : '';
    if (!path) continue;
    // Accept 'type' directly, or fall back to 'language' for backwards compat
    const rawType = dir.type ?? dir.language;
    const type = VALID_TYPES.has(rawType as string) ? (rawType as string) : 'python';
    const entry: DirectoryConfig = { path, type };
    // Support both old (lint_command/build_command/test_command) and new (lint_cmd/build_cmd/test_cmd) field names
    const lintCmd = dir.lint_cmd ?? dir.lint_command;
    const buildCmd = dir.build_cmd ?? dir.build_command;
    const testCmd = dir.test_cmd ?? dir.test_command;
    const installCmd = dir.install_cmd;
    if (typeof lintCmd === 'string') entry.lint_cmd = lintCmd;
    if (typeof buildCmd === 'string') entry.build_cmd = buildCmd;
    if (typeof testCmd === 'string') entry.test_cmd = testCmd;
    if (typeof installCmd === 'string') entry.install_cmd = installCmd;
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

  return config;
}
