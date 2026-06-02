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
 *   - it ALSO adds symbol-level depth: when an entry cites a `path:symbol`
 *     pointer and that symbol is unambiguously absent from a file that still
 *     exists (the re-export-facade / post-refactor-stub case), it is treated
 *     the same as a fully-absent file — mirrors the backend's symbol grounding
 *     in `backend/app/source_ref_validation.py`. Conservative: an indeterminate
 *     read or a present symbol is never flagged,
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

// Recognizes the `path/to/file.ext:symbol` convention used in design-doc
// pointers (e.g. "backend/app/x.py:my_func wires Y"). Mirrors the backend's
// `_PATH_SYMBOL_RE` (backend/app/source_ref_validation.py). The symbol must
// start with a letter or underscore, which excludes line refs like file.py:42.
// Constructed fresh per scan so the global-flag `lastIndex` never leaks.
function pathSymbolRegex(): RegExp {
  return /(?<![\w/])([\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|go|rs)):([A-Za-z_]\w*)/g;
}

// Definition-introducing keywords across Python, TS/JS, Go, and Rust — mirrors
// the backend's `_DEF_KEYWORDS`. A symbol is grounded when any of these
// precedes it, OR when it appears on the left of an assignment / object-key /
// definition. Conservative: only flag absent when NO plausible definition exists.
const DEF_KEYWORDS = [
  'def', 'class', 'function', 'func', 'fn', 'const', 'let', 'var',
  'type', 'interface', 'enum', 'struct', 'trait', 'mod', 'static',
];

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Mirrors the backend's `_symbol_definition_regex`: a definition keyword
// followed by the symbol, or the symbol on the left of an assignment / call /
// object-key definition. The leading `[^\w.]` guard avoids treating
// `obj.symbol` as a definition.
function symbolDefinitionRegex(symbol: string): RegExp {
  const s = escapeRegExp(symbol);
  const kw = `(?:^|[^\\w.])(?:${DEF_KEYWORDS.join('|')})\\s+${s}\\b`;
  const lhs = `(?:^|[^\\w.])${s}\\s*(?:[:=]|\\()`;
  return new RegExp(`${kw}|${lhs}`, 'm');
}

/**
 * Collect deduplicated `path:symbol` pointers cited by an entry — from its
 * `modules` array plus its `notes` / `description` free text.
 */
function extractEntrySymbolRefs(entry: Record<string, unknown>): Array<[string, string]> {
  const sources: string[] = [];
  const modules = entry.modules;
  if (Array.isArray(modules)) {
    for (const m of modules) if (typeof m === 'string' && m) sources.push(m);
  }
  for (const key of ['notes', 'description']) {
    const v = entry[key];
    if (typeof v === 'string' && v) sources.push(v);
  }

  const seen = new Set<string>();
  const pairs: Array<[string, string]> = [];
  for (const text of sources) {
    const re = pathSymbolRegex();
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
      const key = `${match[1]}:${match[2]}`;
      if (!seen.has(key)) {
        seen.add(key);
        pairs.push([match[1], match[2]]);
      }
    }
  }
  return pairs;
}

/**
 * Whether `symbol` is defined in `relPath` under `root`.
 *   true  → file exists and a plausible definition of `symbol` is found.
 *   false → file exists but no definition (unambiguously absent — the facade case).
 *   null  → indeterminate (file unreadable / absent); degrade conservatively.
 * The absent-file case is left to the file-level path check, so it returns null
 * here rather than flagging — symbol-level only adds the present-file-but-gone-
 * symbol depth that the file-existence check misses.
 */
function symbolDefinedInFile(root: string, relPath: string, symbol: string): boolean | null {
  let content: string;
  try {
    content = fs.readFileSync(path.join(root, relPath), 'utf8');
  } catch {
    return null;
  }
  return symbolDefinitionRegex(symbol).test(content);
}

function downgradeEntry(
  entry: Record<string, unknown>,
  kind: EntryKind,
  warning: string,
): Record<string, unknown> {
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
 * Reconcile a single system/capability entry against the workspace file tree.
 * Returns the entry unchanged unless it is a PRESENT-status entry whose backing
 * code is absent — either (file-level) every referenced path is missing, or
 * (symbol-level) a cited `path:symbol` names a symbol unambiguously absent from
 * a file that still exists. Both yield the same `missing` downgrade so every
 * reasoning surface treats it as not-built. Pure: never mutates the input.
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
  // File-level: downgrade only when EVERY referenced path is gone. A partial hit
  // means the code partially exists, so the present-status claim is plausible.
  if (paths.length > 0) {
    const missing = paths.filter((p) => !fs.existsSync(path.join(root, p)));
    if (missing.length === paths.length) {
      return downgradeEntry(
        entry,
        kind,
        `claimed ${status} but all referenced module file(s) are absent on the ` +
          `integration branch — treat as missing`,
      );
    }
  }

  // Symbol-level: a present file can be a re-export facade or post-refactor stub
  // whose cited symbol was deleted/moved. Flag only on an unambiguous absence
  // (file present, no definition) — an indeterminate read is never flagged.
  for (const [refPath, symbol] of extractEntrySymbolRefs(entry)) {
    if (symbolDefinedInFile(root, refPath, symbol) === false) {
      return downgradeEntry(
        entry,
        kind,
        `claimed ${status} but referenced symbol \`${refPath}:${symbol}\` is absent ` +
          `from the present file on the integration branch — treat as missing`,
      );
    }
  }

  return entry;
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
