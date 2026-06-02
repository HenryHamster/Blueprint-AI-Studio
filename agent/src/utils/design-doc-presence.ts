/**
 * Design-doc / problem-model presence reconciliation.
 *
 * Research and strategic agents are handed design-doc *systems* and
 * *capabilities* whose status (mature/functional/partial/stub/missing for
 * systems, working/partial/planned for capabilities) is trusted verbatim.
 * A status authored optimistically — or work that was later reverted / never
 * landed — can mark an entry as PRESENT while the code it references no longer
 * exists on the integration branch. The agent then confabulates the behaviour
 * of nonexistent code and proposes "improving" something never built.
 *
 * This util reconciles those claims against the cloned workspace file tree,
 * mirroring the backend pattern `check_and_flag_stale_refs(doc, repo_root)` in
 * `backend/app/playbook_lifecycle.py`:
 *
 *   - it extracts file-path references from an entry,
 *   - it acts only when the entry carries at least one path pointer AND every
 *     referenced path is confirmed missing (a partial hit means the code
 *     partially exists, so the claim is left alone — conservative),
 *   - entries with no path pointer are left untouched (absence of a path is
 *     not evidence of absent code),
 *   - it is conservative when the workspace root is not a valid directory
 *     (returns input unchanged).
 *
 * When an entry is reconciled it is DOWNGRADED to status `missing` and
 * annotated so the prompt surfaces the correction to the agent.
 */

import fs from 'fs';
import path from 'path';

// Statuses that assert the referenced code is PRESENT (and therefore
// improvable). Anything else (stub/missing/planned) already tells the agent
// to build rather than improve, so it needs no reconciliation.
const PRESENT_SYSTEM_STATUSES = new Set(['mature', 'functional', 'partial']);
const PRESENT_CAPABILITY_STATUSES = new Set(['working', 'partial']);

export type EntryKind = 'system' | 'capability';

// Matches source-file paths embedded in free text — mirrors the backend's
// `_FILE_PATH_RE` (backend/app/playbook_lifecycle.py). Constructed fresh per
// scan so the global-flag `lastIndex` never leaks across calls.
function filePathRegex(): RegExp {
  return /\b((?:backend|agent|admin-ui)\/(?:[\w.-]+\/)*[\w.-]+\.\w+)\b/g;
}

function isValidRoot(root: string | undefined | null): root is string {
  if (!root) return false;
  try {
    return fs.statSync(root).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Collect unique file-path pointers for an entry: its explicit `modules`
 * array (systems) plus any paths embedded in its `notes` / `description`
 * free text (capabilities carry pointers only in `notes`).
 */
export function extractEntryPaths(entry: Record<string, unknown>): string[] {
  const paths: string[] = [];

  const modules = entry.modules;
  if (Array.isArray(modules)) {
    for (const m of modules) {
      if (typeof m === 'string' && m.trim()) paths.push(m.trim());
    }
  }

  const textParts: string[] = [];
  for (const key of ['notes', 'description']) {
    const v = entry[key];
    if (typeof v === 'string' && v) textParts.push(v);
  }
  if (textParts.length > 0) {
    const text = textParts.join('\n');
    const re = filePathRegex();
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
      paths.push(match[1]);
    }
  }

  return [...new Set(paths)];
}

/**
 * Reconcile a single system/capability entry against the workspace file tree.
 * Returns the entry unchanged unless it is a PRESENT-status entry whose every
 * referenced path is absent — in which case a downgraded copy is returned.
 * Pure: never mutates the input.
 */
export function reconcileEntry(
  entry: Record<string, unknown>,
  root: string,
  kind: EntryKind,
): Record<string, unknown> {
  const status = typeof entry.status === 'string' ? entry.status.toLowerCase() : '';
  const presentSet = kind === 'system' ? PRESENT_SYSTEM_STATUSES : PRESENT_CAPABILITY_STATUSES;
  if (!presentSet.has(status)) return entry;

  const paths = extractEntryPaths(entry);
  // No pointer at all → absence of a path is NOT evidence of absent code.
  if (paths.length === 0) return entry;

  const missing = paths.filter((p) => !fs.existsSync(path.join(root, p)));
  // Only downgrade when EVERY referenced path is gone. A partial hit means the
  // code partially exists, so the present-status claim is plausible — leave it.
  if (missing.length !== paths.length) return entry;

  const warning =
    `claimed ${status} but all referenced module file(s) are absent on the ` +
    `integration branch — treat as missing`;
  const downgraded: Record<string, unknown> = {
    ...entry,
    status: 'missing',
    presence_warning: warning,
  };
  const textField = kind === 'capability' ? 'notes' : 'description';
  const rawText = entry[textField];
  const existing = typeof rawText === 'string' ? rawText.trim() : '';
  downgraded[textField] = (existing ? `${existing} ` : '') + `[reconciled: ${warning}]`;
  return downgraded;
}

/**
 * Reconcile a single system entry (the `direction_design_context.system`
 * shape). Conservative no-op when the entry is absent/non-object or the root
 * is invalid.
 */
export function reconcileSystemEntry(
  system: Record<string, unknown> | undefined,
  root: string,
): Record<string, unknown> | undefined {
  if (!system || typeof system !== 'object' || Array.isArray(system)) return system;
  if (!isValidRoot(root)) return system;
  return reconcileEntry(system, root, 'system');
}

/**
 * Reconcile a `direction_design_context.missing_capabilities` map
 * (`{ name: { status, notes } }`). Conservative no-op when the map is
 * absent/non-object or the root is invalid.
 */
export function reconcileMissingCapabilities(
  caps: Record<string, Record<string, string>> | undefined,
  root: string,
): Record<string, Record<string, string>> | undefined {
  if (!caps || typeof caps !== 'object') return caps;
  if (!isValidRoot(root)) return caps;
  const out: Record<string, Record<string, string>> = {};
  for (const [name, cap] of Object.entries(caps as Record<string, unknown>)) {
    out[name] =
      cap && typeof cap === 'object' && !Array.isArray(cap)
        ? (reconcileEntry(cap as Record<string, unknown>, root, 'capability') as Record<string, string>)
        : (cap as Record<string, string>);
  }
  return out;
}

/**
 * Reconcile a full flat-keyed design doc, downgrading PRESENT-but-absent
 * `systems/<id>` and `capabilities/<name>` entries. Other keys (summary,
 * architecture, risks, notes/*, playbook/*, _meta, …) are passed through
 * untouched. Conservative no-op when the doc is absent or the root is invalid.
 * Pure: returns a shallow copy, never mutates the input.
 */
export function reconcileDesignDocPresence(
  doc: Record<string, unknown> | undefined,
  root: string,
): Record<string, unknown> | undefined {
  if (!doc || typeof doc !== 'object') return doc;
  if (!isValidRoot(root)) return doc;

  const out: Record<string, unknown> = { ...doc };
  for (const [key, value] of Object.entries(doc)) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
    if (key.startsWith('systems/')) {
      out[key] = reconcileEntry(value as Record<string, unknown>, root, 'system');
    } else if (key.startsWith('capabilities/')) {
      out[key] = reconcileEntry(value as Record<string, unknown>, root, 'capability');
    }
  }
  return out;
}
