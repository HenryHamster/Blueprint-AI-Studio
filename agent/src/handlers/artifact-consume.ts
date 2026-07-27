import * as fs from 'fs';
import * as path from 'path';
import * as zlib from 'zlib';
import type WebSocket from 'ws';
import { git, sendLog, WORKSPACE } from '../index.js';
import { fetchArtifactDownloadUrl, type PrerequisiteArtifactPointer } from './prior-knowledge.js';

// ── Artifact consume-and-commit toolkit (Pillar 5: SHIP THE ARTIFACT) ────────
//
// prior-knowledge.ts only PASSIVELY renders artifact prerequisites into the
// prompt and exposes fetchArtifactDownloadUrl. This module closes the loop: it
// fetches the signed URL, downloads the `bundle.tar.gz` an upstream task
// delivered, untars it (path-traversal-guarded), extracts the files, and
// git-commits them into the repo clone with a message citing the artifact_id.
// The resulting commit rides the normal review gate via ./pr open.
//
// Everything here is fail-soft by contract: a download/extract/commit miss logs
// and is skipped so the dependent task still runs (the agent can consume the
// signed URL by hand from the prompt section). It is also bounded — size caps
// and an absolute-path / `..` traversal guard — so a hostile or malformed
// archive cannot write outside the repo or exhaust the VM.

// Only artifacts whose type represents committable repo content are auto-
// committed. Report / screenshot / verdict artifacts are informational and
// remain prompt-only — committing them would pollute the diff. GIT_COMMIT is
// the ArtifactType (backend models.py) that denotes delivered repo files.
export const COMMITTABLE_ARTIFACT_TYPES = new Set<string>(['GIT_COMMIT']);
export const HYDRATABLE_ARTIFACT_TYPES = new Set<string>(['COMPUTE_RESULT']);

// Cap on the compressed bundle we will download. Bundles are small file sets;
// anything larger is almost certainly wrong and we refuse it rather than risk
// OOM on a 2-vCPU VM.
export const MAX_BUNDLE_DOWNLOAD_BYTES = 100 * 1024 * 1024; // 100 MiB compressed
export const BUNDLE_DOWNLOAD_TIMEOUT_MS = 30_000;

export interface ExtractLimits {
  /** Maximum number of files written from a single bundle. */
  maxFiles: number;
  /** Maximum decompressed total bytes written from a single bundle. */
  maxTotalBytes: number;
  /** Maximum decompressed bytes for any single entry. */
  maxEntryBytes: number;
}

export const DEFAULT_EXTRACT_LIMITS: ExtractLimits = {
  maxFiles: 2000,
  maxTotalBytes: 50 * 1024 * 1024, // 50 MiB decompressed
  maxEntryBytes: 25 * 1024 * 1024, // 25 MiB per file
};

export interface TarEntry {
  name: string;
  type: 'file' | 'dir' | 'other';
  data: Buffer;
}

// Read a tar header numeric field. tar stores numbers as NUL/space-terminated
// octal ASCII; GNU also uses a base-256 encoding (high bit of the first byte
// set) for values that don't fit the octal width. Handle both.
function readOctal(buf: Buffer, offset: number, length: number): number {
  if (offset + length > buf.length) return 0;
  if ((buf[offset] & 0x80) !== 0) {
    let val = 0;
    for (let i = offset; i < offset + length; i++) {
      const byte = i === offset ? buf[i] & 0x7f : buf[i];
      val = val * 256 + byte;
    }
    return val;
  }
  const raw = buf.toString('ascii', offset, offset + length).replace(/\0/g, ' ').trim();
  if (!raw) return 0;
  const n = parseInt(raw, 8);
  return Number.isFinite(n) ? n : 0;
}

function stripNul(s: string): string {
  const i = s.indexOf('\0');
  return i === -1 ? s : s.slice(0, i);
}

// Parse a (decompressed) tar archive into entries. Handles the ustar format
// plus the GNU long-name ('L') and pax extended-header ('x'/'g' with a `path=`
// record) extensions that GNU `tar -czf` can emit for long paths. Symlinks and
// other special members are surfaced as type 'other' and never extracted.
export function parseTar(buf: Buffer): TarEntry[] {
  const entries: TarEntry[] = [];
  let offset = 0;
  let pendingName: string | null = null;
  while (offset + 512 <= buf.length) {
    const block = buf.subarray(offset, offset + 512);
    let allZero = true;
    for (let i = 0; i < 512; i++) {
      if (block[i] !== 0) { allZero = false; break; }
    }
    if (allZero) break; // end-of-archive marker

    const name = stripNul(block.toString('utf-8', 0, 100));
    const prefix = stripNul(block.toString('utf-8', 345, 500));
    const typeflag = String.fromCharCode(block[156]);
    const size = readOctal(block, 124, 12);

    offset += 512;
    const dataStart = offset;
    const dataEnd = Math.min(dataStart + size, buf.length);
    offset += Math.ceil(size / 512) * 512;

    if (typeflag === 'L') {
      // GNU long name: this block's payload is the name of the NEXT entry.
      pendingName = stripNul(buf.toString('utf-8', dataStart, dataEnd));
      continue;
    }
    if (typeflag === 'K') {
      // GNU long link target — irrelevant for extraction.
      continue;
    }
    if (typeflag === 'x' || typeflag === 'g') {
      const header = buf.toString('utf-8', dataStart, dataEnd);
      const match = header.match(/\d+ path=([^\n]*)\n/);
      if (match) pendingName = match[1];
      continue;
    }

    const fullName = pendingName ?? (prefix ? `${prefix}/${name}` : name);
    pendingName = null;

    if (typeflag === '5') {
      entries.push({ name: fullName, type: 'dir', data: Buffer.alloc(0) });
    } else if (typeflag === '0' || typeflag === '\0' || block[156] === 0) {
      entries.push({ name: fullName, type: 'file', data: buf.subarray(dataStart, dataEnd) });
    } else {
      entries.push({ name: fullName, type: 'other', data: Buffer.alloc(0) });
    }
  }
  return entries;
}

// Reject archive entry names that would escape the extraction root: absolute
// paths (POSIX or Windows-drive), and any path containing a `..` segment.
export function isUnsafeEntryPath(name: string): boolean {
  if (!name) return true;
  const normalized = name.replace(/\\/g, '/');
  if (normalized.startsWith('/')) return true;
  if (/^[A-Za-z]:\//.test(normalized)) return true; // Windows drive letter
  return normalized.split('/').some((segment) => segment === '..');
}

export interface ExtractResult {
  /** Repo-relative paths written. */
  written: string[];
  /** Entry names rejected by the path-traversal / absolute-path guard. */
  rejected: string[];
  /** Entries skipped: non-file members or over a size/count cap. */
  skipped: string[];
}

// Decompress and extract a `bundle.tar.gz` buffer into destDir, enforcing the
// path-traversal guard and the size/count caps. Only regular files are written;
// directories are created implicitly, symlinks/specials are skipped.
export function extractTarGzToDir(
  gzBuf: Buffer,
  destDir: string,
  limits: ExtractLimits = DEFAULT_EXTRACT_LIMITS,
): ExtractResult {
  // Include bounded tar header/padding overhead while preventing a compressed
  // zip bomb from being expanded without limit before per-entry checks run.
  // tar writers commonly pad small archives to a 10 KiB record boundary.
  // Account for that fixed envelope plus one overflow entry so we can report
  // the max-files violation without making decompression unbounded.
  const maxTarBytes = limits.maxTotalBytes + 10240 + ((limits.maxFiles + 1) * 1024);
  const tarBuf = zlib.gunzipSync(gzBuf, { maxOutputLength: maxTarBytes });
  const entries = parseTar(tarBuf);
  const result: ExtractResult = { written: [], rejected: [], skipped: [] };
  const resolvedRoot = path.resolve(destDir);
  let totalBytes = 0;

  for (const entry of entries) {
    if (entry.type === 'dir') continue;
    if (entry.type !== 'file') { result.skipped.push(entry.name); continue; }
    if (isUnsafeEntryPath(entry.name)) { result.rejected.push(entry.name); continue; }

    const cleaned = entry.name.replace(/^\.\//, '').replace(/\/+$/, '');
    if (!cleaned) { result.skipped.push(entry.name); continue; }
    if (result.written.length >= limits.maxFiles) { result.skipped.push(entry.name); continue; }
    if (entry.data.length > limits.maxEntryBytes) { result.skipped.push(entry.name); continue; }
    if (totalBytes + entry.data.length > limits.maxTotalBytes) { result.skipped.push(entry.name); continue; }

    const target = path.resolve(resolvedRoot, cleaned);
    // Defense-in-depth: even after the name guard, confirm the resolved path is
    // inside the root before any write touches disk.
    if (target !== resolvedRoot && !target.startsWith(resolvedRoot + path.sep)) {
      result.rejected.push(entry.name);
      continue;
    }
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, entry.data);
    totalBytes += entry.data.length;
    result.written.push(cleaned);
  }
  return result;
}

// Download a signed-URL bundle to memory, bounded. Returns null fail-soft on any
// network error, non-2xx status, empty body, or over-cap size.
export async function downloadBundle(
  url: string,
  maxBytes: number = MAX_BUNDLE_DOWNLOAD_BYTES,
  timeoutMs: number = BUNDLE_DOWNLOAD_TIMEOUT_MS,
): Promise<Buffer | null> {
  const controller = new AbortController();
  const deadline = setTimeout(() => {
    controller.abort();
  }, timeoutMs);
  deadline.unref();
  try {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) return null;
    const lengthHeader = res.headers.get('content-length');
    if (lengthHeader && Number(lengthHeader) > maxBytes) return null;
    if (!res.body) return null;
    const reader = res.body.getReader();
    const chunks: Buffer[] = [];
    let totalBytes = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value.byteLength === 0) continue;
      totalBytes += value.byteLength;
      if (totalBytes > maxBytes) {
        try { await reader.cancel(); } catch { /* best-effort connection cleanup */ }
        return null;
      }
      chunks.push(Buffer.from(value));
    }
    if (totalBytes === 0) return null;
    return Buffer.concat(chunks, totalBytes);
  } catch {
    return null;
  } finally {
    clearTimeout(deadline);
  }
}

// Merge artifact ids into /tmp/consumed_artifacts_<taskId>.json so auto-consumed
// artifacts are credited under the same convention the prompt asks the agent to
// follow. Union with any pre-existing array so we never drop ids the agent (or a
// prior consume pass) already recorded.
export function recordConsumedArtifacts(taskId: string, ids: string[]): void {
  if (ids.length === 0) return;
  const file = `/tmp/consumed_artifacts_${taskId}.json`;
  try {
    let existing: string[] = [];
    if (fs.existsSync(file)) {
      const parsed: unknown = JSON.parse(fs.readFileSync(file, 'utf-8'));
      if (Array.isArray(parsed)) existing = parsed.filter((x): x is string => typeof x === 'string');
    }
    const merged = Array.from(new Set([...existing, ...ids]));
    fs.writeFileSync(file, JSON.stringify(merged), { mode: 0o644 });
  } catch {
    /* non-fatal: crediting is best-effort */
  }
}

export interface ConsumeArtifactsOptions {
  taskId: string;
  pointers: PrerequisiteArtifactPointer[];
  /** Repo clone to commit into; defaults to the agent workspace. */
  repoRoot?: string;
  ws?: WebSocket | null;
  /** Pre-resolved signed URLs (e.g. from the prompt-section pass) to avoid a
   *  second download-url mint. Falls back to fetchArtifactDownloadUrl on a miss. */
  signedUrls?: Map<string, string>;
  limits?: ExtractLimits;
  // Injection points for tests.
  resolveUrl?: (artifactId: string) => Promise<string | null>;
  download?: (url: string) => Promise<Buffer | null>;
}

export interface ArtifactCommit {
  artifact_id: string;
  files: string[];
  sha: string;
}

export interface ConsumeOutcome {
  committedArtifactIds: string[];
  commits: ArtifactCommit[];
  rejectedEntries: { artifact_id: string; entries: string[] }[];
}

export interface HydrateArtifactsOptions {
  pointers: PrerequisiteArtifactPointer[];
  /** Repo clone beside which the ignored runtime input directory is created. */
  repoRoot?: string;
  ws?: WebSocket | null;
  signedUrls?: Map<string, string>;
  limits?: ExtractLimits;
  resolveUrl?: (artifactId: string) => Promise<string | null>;
  download?: (url: string) => Promise<Buffer | null>;
  /** Test injection; production writes the repository-local git exclude. */
  ensureIgnored?: (repoRoot: string) => boolean;
}

export interface HydrateOutcome {
  /** Artifact id -> repo-relative directory containing the extracted files. */
  localPaths: Map<string, string>;
  rejectedEntries: { artifact_id: string; entries: string[] }[];
}

function safePathComponent(value: string): string {
  return value.replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+$/, '_').slice(0, 160);
}

function hasSymlinkInExistingPath(root: string, relativePath: string): boolean {
  let cursor = path.resolve(root);
  for (const component of relativePath.split(/[\\/]+/).filter(Boolean)) {
    cursor = path.join(cursor, component);
    if (fs.existsSync(cursor) && fs.lstatSync(cursor).isSymbolicLink()) return true;
  }
  return false;
}

export function computeArtifactInputPath(pointer: PrerequisiteArtifactPointer): string {
  const uriMatch = (pointer.storage_uri || '').match(/(?:^|\/)compute-jobs\/([^/]+)/);
  const sourceId = pointer.source_compute_job_id || uriMatch?.[1] || pointer.artifact_id;
  return path.posix.join('.blueprint-inputs', 'compute-jobs', safePathComponent(sourceId || 'unknown'));
}

// Runtime inputs must never be swept into `git add -A`. Use the clone-local
// exclude instead of editing the consumer repository's tracked .gitignore.
export function ensureRuntimeInputsIgnored(repoRoot: string): boolean {
  try {
    const rawGitPath = git(['rev-parse', '--git-path', 'info/exclude'], repoRoot).trim();
    if (!rawGitPath) return false;
    const excludePath = path.isAbsolute(rawGitPath) ? rawGitPath : path.resolve(repoRoot, rawGitPath);
    fs.mkdirSync(path.dirname(excludePath), { recursive: true });
    const current = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
    if (!current.split(/\r?\n/).includes('/.blueprint-inputs/')) {
      fs.appendFileSync(excludePath, `${current && !current.endsWith('\n') ? '\n' : ''}/.blueprint-inputs/\n`);
    }
    git(
      ['check-ignore', '-q', '--no-index', '--', '.blueprint-inputs/.blueprint-ignore-probe'],
      repoRoot,
    );
    return true;
  } catch {
    return false;
  }
}

// Fetch and safely unpack COMPUTE_RESULT prerequisites into a predictable,
// ignored workspace directory. Unlike GIT_COMMIT consumption, this never stages
// or commits anything. Failures remain prompt-visible through the signed URL.
export async function hydrateComputeResultArtifacts(opts: HydrateArtifactsOptions): Promise<HydrateOutcome> {
  const repoRoot = opts.repoRoot ?? WORKSPACE;
  const ws = opts.ws ?? null;
  const limits = opts.limits ?? DEFAULT_EXTRACT_LIMITS;
  const resolveUrl = opts.resolveUrl ?? fetchArtifactDownloadUrl;
  const download = opts.download ?? downloadBundle;
  const ensureIgnored = opts.ensureIgnored ?? ensureRuntimeInputsIgnored;
  const outcome: HydrateOutcome = { localPaths: new Map(), rejectedEntries: [] };
  const pointers = opts.pointers.filter(
    (p) => (p.artifact_id || '').trim() && HYDRATABLE_ARTIFACT_TYPES.has((p.artifact_type || '').trim()),
  );
  if (pointers.length === 0) return outcome;

  if (!ensureIgnored(repoRoot)) {
    sendLog(ws, '[ArtifactHydrate] Could not install the local git exclude; refusing to create untracked compute inputs.\n');
    return outcome;
  }

  for (const pointer of pointers) {
    const id = pointer.artifact_id.trim();
    const relativeDir = computeArtifactInputPath(pointer);
    const destDir = path.resolve(repoRoot, relativeDir);
    let tempDir: string | null = null;
    try {
      const url = opts.signedUrls?.get(id) || (await resolveUrl(id));
      if (!url) {
        sendLog(ws, `[ArtifactHydrate] No signed URL for compute artifact ${id} — leaving it prompt-only.\n`);
        continue;
      }
      const buf = await download(url);
      if (!buf) {
        sendLog(ws, `[ArtifactHydrate] Could not download compute artifact ${id} — leaving it prompt-only.\n`);
        continue;
      }

      // The destination is derived only from a sanitized id. Refuse a tracked
      // symlink at any runtime-input path component before recursive cleanup,
      // then replace the destination as a unit so retries cannot mix versions.
      if (hasSymlinkInExistingPath(repoRoot, relativeDir)) {
        sendLog(ws, `[ArtifactHydrate] Refusing compute artifact ${id}: runtime input path contains a symlink.\n`);
        continue;
      }
      fs.mkdirSync(path.dirname(destDir), { recursive: true });
      tempDir = fs.mkdtempSync(`${destDir}.tmp-`);
      const contentsDir = path.join(tempDir, 'contents');
      fs.mkdirSync(contentsDir, { recursive: true });
      // Preserve the exact downloaded bytes for tarball-aware ingest and make
      // a separate, safely extracted view available for ordinary file reads.
      fs.writeFileSync(path.join(tempDir, 'bundle.tar.gz'), buf);
      const extracted = extractTarGzToDir(buf, contentsDir, limits);
      if (extracted.rejected.length > 0) {
        outcome.rejectedEntries.push({ artifact_id: id, entries: extracted.rejected });
        sendLog(ws, `[ArtifactHydrate] Rejected ${extracted.rejected.length} unsafe entr(ies) in compute artifact ${id}: ${extracted.rejected.slice(0, 5).join(', ')}\n`);
      }
      if (extracted.written.length === 0) {
        fs.rmSync(tempDir, { recursive: true, force: true });
        tempDir = null;
        sendLog(ws, `[ArtifactHydrate] Compute artifact ${id} contained no usable files.\n`);
        continue;
      }
      fs.rmSync(destDir, { recursive: true, force: true });
      fs.renameSync(tempDir, destDir);
      tempDir = null;
      outcome.localPaths.set(id, relativeDir);
      sendLog(ws, `[ArtifactHydrate] Extracted ${extracted.written.length} compute artifact file(s) to ${relativeDir}/ (not staged).\n`);
    } catch (e) {
      if (tempDir) fs.rmSync(tempDir, { recursive: true, force: true });
      sendLog(ws, `[ArtifactHydrate] Failed to hydrate compute artifact ${id} (non-fatal): ${e instanceof Error ? e.message : String(e)}\n`);
    }
  }
  return outcome;
}

// Fetch → untar → extract → git-commit each committable delivered artifact into
// the repo clone. One commit per artifact, message citing its artifact_id.
// Fully fail-soft: a failure on one artifact is logged and the loop continues.
export async function consumeAndCommitArtifacts(opts: ConsumeArtifactsOptions): Promise<ConsumeOutcome> {
  const repoRoot = opts.repoRoot ?? WORKSPACE;
  const ws = opts.ws ?? null;
  const limits = opts.limits ?? DEFAULT_EXTRACT_LIMITS;
  const resolveUrl = opts.resolveUrl ?? fetchArtifactDownloadUrl;
  const download = opts.download ?? downloadBundle;
  const outcome: ConsumeOutcome = { committedArtifactIds: [], commits: [], rejectedEntries: [] };

  const committable = opts.pointers.filter(
    (p) => (p.artifact_id || '').trim() && COMMITTABLE_ARTIFACT_TYPES.has((p.artifact_type || '').trim()),
  );
  if (committable.length === 0) return outcome;

  for (const pointer of committable) {
    const id = pointer.artifact_id.trim();
    try {
      const url = opts.signedUrls?.get(id) || (await resolveUrl(id));
      if (!url) {
        sendLog(ws, `[ArtifactConsume] No signed URL for artifact ${id} — skipping.\n`);
        continue;
      }
      const buf = await download(url);
      if (!buf) {
        sendLog(ws, `[ArtifactConsume] Could not download bundle for artifact ${id} — skipping.\n`);
        continue;
      }

      let extracted: ExtractResult;
      try {
        extracted = extractTarGzToDir(buf, repoRoot, limits);
      } catch (e) {
        sendLog(ws, `[ArtifactConsume] Failed to untar artifact ${id}: ${e instanceof Error ? e.message : String(e)} — skipping.\n`);
        continue;
      }
      if (extracted.rejected.length > 0) {
        outcome.rejectedEntries.push({ artifact_id: id, entries: extracted.rejected });
        sendLog(ws, `[ArtifactConsume] Rejected ${extracted.rejected.length} unsafe entr(ies) in artifact ${id}: ${extracted.rejected.slice(0, 5).join(', ')}\n`);
      }
      if (extracted.written.length === 0) {
        sendLog(ws, `[ArtifactConsume] Artifact ${id} contained no committable files — skipping.\n`);
        continue;
      }

      git(['add', '--', ...extracted.written], repoRoot);
      const staged = git(['diff', '--cached', '--name-only'], repoRoot);
      if (!staged) {
        sendLog(ws, `[ArtifactConsume] Artifact ${id} extracted no net-new content (already in repo) — skipping commit.\n`);
        continue;
      }
      const label = (pointer.logical_key || pointer.artifact_type || 'artifact').trim();
      const source = pointer.source_task_id ? ` from task ${pointer.source_task_id}` : '';
      const message =
        `chore: commit delivered artifact ${id}\n\n` +
        `Consumes prerequisite artifact ${id} (${label})${source}.\n` +
        `Extracted ${extracted.written.length} file(s) from bundle.tar.gz.`;
      git(['commit', '--no-verify', '-m', message], repoRoot);
      const sha = git(['rev-parse', 'HEAD'], repoRoot);

      outcome.committedArtifactIds.push(id);
      outcome.commits.push({ artifact_id: id, files: extracted.written, sha });
      sendLog(ws, `[ArtifactConsume] Committed ${extracted.written.length} file(s) from artifact ${id} (${sha.slice(0, 8)}).\n`);
    } catch (e) {
      sendLog(ws, `[ArtifactConsume] consume of artifact ${id} failed (non-fatal): ${e instanceof Error ? e.message : String(e)}\n`);
    }
  }

  recordConsumedArtifacts(opts.taskId, outcome.committedArtifactIds);
  return outcome;
}
