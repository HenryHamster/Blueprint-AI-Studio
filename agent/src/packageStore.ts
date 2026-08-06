// Shared pip/uv package store for runner workspaces.
//
// Every per-workspace dependency install (pip/uv) used to populate its own
// cache from the network on each VM. This module points pip and uv at a shared
// store under the same root the image bakes for npm and the git object mirror
// (/opt/agent-cache) so a long-lived runner host downloads and re-materializes
// each package once, then every later workspace reads it through the store.
//
// Isolation semantics:
//   - The store holds ONLY immutable content-addressed artifacts (pip's wheel /
//     HTTP cache, uv's archive/wheel cache). pip and uv both write via
//     temp-file + atomic rename and never mutate a cache entry, so concurrent
//     workspaces reading and writing the same store cannot corrupt it or each
//     other.
//   - A workspace's RESOLVED environment (its venv) lives INSIDE that
//     workspace (see `resolvedEnvPath`) and is never placed in the shared
//     store, so a workspace cannot see or mutate another workspace's resolved
//     environment through the store.
//
// The store is bounded: `enforcePackageStoreBounds` evicts the oldest files
// (by mtime) once the store exceeds a size cap, so the shared store cannot
// grow without limit on a long-lived runner host.
import fs from 'fs';
import path from 'path';

/** Env override for the shared store root (mirrors GIT_REFERENCE_CACHE_ROOT). */
export const PACKAGE_STORE_ROOT_ENV = 'BLUEPRINT_PACKAGE_STORE_ROOT';
/** Env override for the eviction size cap, in bytes. */
export const PACKAGE_STORE_MAX_BYTES_ENV = 'BLUEPRINT_PACKAGE_STORE_MAX_BYTES';

/** Same root the image bakes the npm cache and git mirror under. */
export const DEFAULT_PACKAGE_STORE_ROOT = '/opt/agent-cache';
/** Default store cap: 4 GiB of immutable package artifacts per host. */
export const DEFAULT_PACKAGE_STORE_MAX_BYTES = 4 * 1024 * 1024 * 1024;
/**
 * Eviction never deletes a file touched within this window, so an in-flight
 * download (which pip/uv write as a temp file + atomic rename) is not torn.
 */
export const DEFAULT_EVICTION_GRACE_MS = 30 * 60 * 1000;

export interface PackageStoreDirs {
  root: string;
  pip: string;
  uv: string;
}

export interface PackageStoreEvictionResult {
  removedFiles: number;
  reclaimedBytes: number;
  totalBytesAfter: number;
}

/** Resolve the shared store root from the env, falling back to the baked root. */
export function resolvePackageStoreRoot(): string {
  return process.env[PACKAGE_STORE_ROOT_ENV] || DEFAULT_PACKAGE_STORE_ROOT;
}

/** The pip and uv cache directories that make up the shared store. */
export function packageStoreDirs(root?: string): PackageStoreDirs {
  const base = root ?? resolvePackageStoreRoot();
  return { root: base, pip: path.join(base, 'pip'), uv: path.join(base, 'uv') };
}

/** Resolve the store size cap from the env, falling back to the default. */
export function packageStoreMaxBytes(): number {
  const raw = process.env[PACKAGE_STORE_MAX_BYTES_ENV];
  if (raw) {
    const n = Number(raw);
    if (Number.isFinite(n) && n > 0) return Math.floor(n);
  }
  return DEFAULT_PACKAGE_STORE_MAX_BYTES;
}

/**
 * Where a workspace's RESOLVED python environment lives. This is per-workspace
 * and sits INSIDE the workspace — the isolation contract that keeps one
 * workspace from seeing or mutating another's resolved environment is that the
 * shared store never contains (and nothing ever points at) this path.
 */
export function resolvedEnvPath(workspace: string): string {
  return path.join(workspace, '.venv');
}

/**
 * Point pip and uv at the shared store for this process and every dependency-
 * install child process spawned afterwards. Best-effort: a store that cannot
 * be created (e.g. a read-only /opt on a dev box) is skipped and pip/uv fall
 * back to their default per-workspace caching.
 */
export function configurePackageStoreEnv(): PackageStoreDirs {
  const dirs = packageStoreDirs();
  for (const dir of [dirs.pip, dirs.uv]) {
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch {
      // Best-effort — the store is an optimization, not a correctness dependency.
    }
  }
  process.env.PIP_CACHE_DIR = dirs.pip;
  process.env.UV_CACHE_DIR = dirs.uv;
  return dirs;
}

interface StoreFile {
  path: string;
  size: number;
  mtimeMs: number;
}

/** Recursively collect regular files (path, size, mtime) under a store dir. */
function collectStoreFiles(dir: string, out: StoreFile[]): void {
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    // Absent or unreadable store dir — nothing to evict.
    return;
  }
  for (const entry of entries) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      collectStoreFiles(p, out);
    } else if (entry.isFile()) {
      try {
        const st = fs.statSync(p);
        out.push({ path: p, size: st.size, mtimeMs: st.mtimeMs });
      } catch {
        // Raced with an eviction or a tool removing the entry.
      }
    }
  }
}

/**
 * Keep the shared store bounded. When the combined pip + uv trees exceed
 * `maxBytes`, delete the oldest files (by mtime) until the store is under the
 * cap. Files touched within `graceMs` are left alone so an in-flight download
 * is never torn. Returns what was reclaimed.
 */
export function enforcePackageStoreBounds(opts?: {
  maxBytes?: number;
  graceMs?: number;
  nowMs?: number;
  dirs?: PackageStoreDirs;
}): PackageStoreEvictionResult {
  const dirs = opts?.dirs ?? packageStoreDirs();
  const maxBytes = opts?.maxBytes ?? packageStoreMaxBytes();
  const graceMs = opts?.graceMs ?? DEFAULT_EVICTION_GRACE_MS;
  const now = opts?.nowMs ?? Date.now();

  const files: StoreFile[] = [];
  if (fs.existsSync(dirs.pip)) collectStoreFiles(dirs.pip, files);
  if (fs.existsSync(dirs.uv)) collectStoreFiles(dirs.uv, files);

  const total = files.reduce((sum, f) => sum + f.size, 0);
  if (total <= maxBytes) {
    return { removedFiles: 0, reclaimedBytes: 0, totalBytesAfter: total };
  }

  const evictable = files
    .filter((f) => now - f.mtimeMs >= graceMs)
    .sort((a, b) => a.mtimeMs - b.mtimeMs);

  let removedFiles = 0;
  let reclaimedBytes = 0;
  for (const f of evictable) {
    if (total - reclaimedBytes <= maxBytes) break;
    try {
      fs.rmSync(f.path, { force: true });
      removedFiles += 1;
      reclaimedBytes += f.size;
    } catch {
      // Raced — another eviction or a concurrent workspace already removed it.
    }
  }
  return { removedFiles, reclaimedBytes, totalBytesAfter: total - reclaimedBytes };
}
