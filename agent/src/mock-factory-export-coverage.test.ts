import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync, existsSync, writeFileSync } from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

/**
 * Guard: full-replacement vi.mock factories in the AGENT suite must cover all
 * runtime exports of the real internal modules they replace.
 *
 * API-mock drift — a test's vi.mock factory falling out of sync with the real
 * module's exports — is one of this project's proven promotion-pileup classes,
 * and it explicitly includes the agent stack (not just admin-ui). This guard is
 * the agent-side mirror of admin-ui/src/test/mock-factory-export-coverage.test.ts.
 *
 * How it works:
 *   1. Walk agent/src/**\/*.test.ts and find every vi.mock('<spec>', factory).
 *   2. Keep only specs that resolve to a REAL internal agent module — a relative
 *      './x.js' / '../y/z.js' specifier that maps (NodeNext: .js -> .ts) to an
 *      existing source file under src/. Node builtins and bare deps (fs,
 *      child_process, os, ws, path, ...) are excluded — those are legitimately
 *      partially mocked.
 *   3. Skip factories that safely delegate to the original module
 *      (importOriginal / importActual / `...actual` spread) — those cannot drift.
 *   4. For each remaining FULL-REPLACEMENT factory, load the real module's
 *      runtime exports (dynamic import is authoritative — it captures
 *      `export { a, b }` re-exports that a text regex misses — with a text
 *      fallback) and diff them against the factory's top-level keys.
 *   5. Fail when a real export is missing from the factory AND is not in the
 *      grandfathering BASELINE. So a NEWLY-added real export can't silently be
 *      absent from a mock (drift), while the large set of intentionally-partial
 *      existing mocks stays green.
 *
 * The BASELINE (mock-factory-export-coverage.baseline.json, kept as a sibling
 * data file per this repo's config-as-JSON convention) records the exports each
 * existing partial full-replacement mock currently omits. Regenerate it after
 * intentionally changing a mock with:
 *   DUMP_MOCK_COVERAGE_BASELINE=/tmp/live.json npx vitest run src/mock-factory-export-coverage.test.ts
 * then replace the JSON file with /tmp/live.json's contents (keeping the _readme key).
 */

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)));

// ---------------------------------------------------------------------------
// fs utilities
// ---------------------------------------------------------------------------

function walkTestFiles(dir: string): string[] {
  const results: string[] = [];
  let entries: string[];
  try { entries = readdirSync(dir); } catch { return results; }
  for (const entry of entries) {
    const full = path.join(dir, entry);
    try {
      const stat = statSync(full);
      if (stat.isDirectory()) {
        results.push(...walkTestFiles(full));
      } else if (/\.test\.ts$/.test(entry)) {
        results.push(full);
      }
    } catch { /* skip unreadable entries */ }
  }
  return results;
}

// ---------------------------------------------------------------------------
// Internal-module resolution (NodeNext: relative './x.js' -> real src/x.ts)
// ---------------------------------------------------------------------------

/**
 * Resolve a vi.mock() specifier to a real internal source file, or null when the
 * specifier is a node builtin / bare dependency / non-existent path. Only
 * relative specifiers that map to an existing .ts under src/ are internal.
 */
export function resolveInternalModule(testFile: string, spec: string): string | null {
  if (!spec.startsWith('.')) return null; // node builtin or bare dep -> partial mocks are legitimate
  const base = path.resolve(path.dirname(testFile), spec);
  const candidates: string[] = [];
  if (base.endsWith('.js')) {
    candidates.push(base.slice(0, -3) + '.ts');
    candidates.push(base.slice(0, -3) + '.tsx');
  }
  candidates.push(base + '.ts');
  candidates.push(path.join(base, 'index.ts'));
  for (const candidate of candidates) {
    const rel = path.relative(SRC_DIR, candidate);
    if (rel.startsWith('..') || path.isAbsolute(rel)) continue; // outside src/
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Runtime export extraction
// Dynamic import is authoritative (captures `export { a, b }` re-exports and
// excludes `export type`); text extraction is the fallback.
// ---------------------------------------------------------------------------

export function extractRuntimeExportsFromText(filePath: string): Set<string> {
  let src: string;
  try { src = readFileSync(filePath, 'utf-8'); } catch { return new Set(); }
  const names = new Set<string>();
  // export [async] function/const/let/class Foo  (skips interface/type/abstract)
  const declRe = /^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm;
  let m: RegExpExecArray | null;
  while ((m = declRe.exec(src)) !== null) names.add(m[1]);
  // export { a, b as c } [from '...']  — value re-exports; skip `export type {`
  const braceRe = /^export\s+(?!type\b)\{([^}]*)\}/gm;
  while ((m = braceRe.exec(src)) !== null) {
    for (const raw of m[1].split(',')) {
      const part = raw.trim();
      if (!part || /^type\b/.test(part)) continue; // inline `type X` specifier
      const asMatch = /\bas\s+([A-Za-z_$][\w$]*)\s*$/.exec(part);
      const name = asMatch ? asMatch[1] : part;
      if (/^[A-Za-z_$][\w$]*$/.test(name)) names.add(name);
    }
  }
  return names;
}

async function loadRuntimeExports(tsPath: string): Promise<Set<string>> {
  try {
    const mod = (await import(pathToFileURL(tsPath).href)) as Record<string, unknown>;
    const keys = Object.keys(mod).filter(k => k !== 'default' && k !== '__esModule');
    if (keys.length > 0) return new Set(keys);
  } catch { /* fall through to text extraction */ }
  return extractRuntimeExportsFromText(tsPath);
}

// ---------------------------------------------------------------------------
// Balanced-delimiter parsing (ported from admin-ui scanner)
// ---------------------------------------------------------------------------

function skipWhitespaceAndComments(text: string, index: number): number {
  let i = index;
  while (i < text.length) {
    if (/\s/.test(text[i] ?? '')) { i++; continue; }
    if (text[i] === '/' && text[i + 1] === '/') {
      i += 2;
      while (i < text.length && text[i] !== '\n') i++;
      continue;
    }
    if (text[i] === '/' && text[i + 1] === '*') {
      i += 2;
      while (i < text.length - 1 && !(text[i] === '*' && text[i + 1] === '/')) i++;
      i += 2;
      continue;
    }
    break;
  }
  return i;
}

function stripWrappingParens(text: string): string {
  let trimmed = text.trim();
  while (trimmed.startsWith('(') && trimmed.endsWith(')')) {
    let depth = 0;
    let inString = false;
    let stringChar = '';
    let inLineComment = false;
    let inBlockComment = false;
    let wrapsWholeText = true;

    for (let i = 0; i < trimmed.length; i++) {
      const ch = trimmed[i];
      if (inLineComment) { if (ch === '\n') inLineComment = false; continue; }
      if (inBlockComment) {
        if (ch === '*' && trimmed[i + 1] === '/') { inBlockComment = false; i++; }
        continue;
      }
      if (inString) {
        if (ch === '\\') { i++; continue; }
        if (ch === stringChar) inString = false;
        continue;
      }
      if (ch === '/' && trimmed[i + 1] === '/') { inLineComment = true; i++; continue; }
      if (ch === '/' && trimmed[i + 1] === '*') { inBlockComment = true; i++; continue; }
      if (ch === '"' || ch === "'" || ch === '`') { inString = true; stringChar = ch; continue; }
      if (ch === '(') { depth++; continue; }
      if (ch === ')') {
        depth--;
        if (depth === 0 && i !== trimmed.length - 1) { wrapsWholeText = false; break; }
      }
    }

    if (!wrapsWholeText) break;
    trimmed = trimmed.slice(1, -1).trim();
  }
  return trimmed;
}

function startsWithWord(text: string, index: number, word: string): boolean {
  if (!text.startsWith(word, index)) return false;
  const before = text.charAt(index - 1); // '' when out of bounds
  const after = text.charAt(index + word.length);
  return !(/[A-Za-z0-9_$]/.test(before)) && !(/[A-Za-z0-9_$]/.test(after));
}

function readDelimitedExpression(text: string, start: number, terminators: Set<string>): { expression: string; nextIndex: number } {
  let parenDepth = 0;
  let bracketDepth = 0;
  let braceDepth = 0;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (inLineComment) { if (ch === '\n') inLineComment = false; continue; }
    if (inBlockComment) {
      if (ch === '*' && text[i + 1] === '/') { inBlockComment = false; i++; }
      continue;
    }
    if (inString) {
      if (ch === '\\') { i++; continue; }
      if (ch === stringChar) inString = false;
      continue;
    }
    if (ch === '/' && text[i + 1] === '/') { inLineComment = true; i++; continue; }
    if (ch === '/' && text[i + 1] === '*') { inBlockComment = true; i++; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { inString = true; stringChar = ch; continue; }
    if (ch === '(') { parenDepth++; continue; }
    if (ch === ')') { if (parenDepth > 0) { parenDepth--; continue; } }
    if (ch === '[') { bracketDepth++; continue; }
    if (ch === ']') { if (bracketDepth > 0) { bracketDepth--; continue; } }
    if (ch === '{') { braceDepth++; continue; }
    if (ch === '}') {
      if (braceDepth > 0) {
        braceDepth--;
        if (braceDepth === 0 && terminators.has('}')) {
          return { expression: text.slice(start, i).trim(), nextIndex: i };
        }
        continue;
      }
    }
    if (parenDepth === 0 && bracketDepth === 0 && braceDepth === 0 && terminators.has(ch)) {
      return { expression: text.slice(start, i).trim(), nextIndex: i };
    }
  }
  return { expression: text.slice(start).trim(), nextIndex: text.length };
}

/**
 * A factory expression is "delegating" (cannot drift) when it returns the
 * original module directly: an importOriginal()/importActual() call, or an alias
 * bound to one. The agent suite has no centralized complete-mock helpers, so
 * there is no helper-call form to recognize here (unlike admin-ui).
 */
function isDelegatingSource(expression: string, aliases: Set<string>): boolean {
  const body = stripWrappingParens(expression);
  if (!body) return false;
  if (/^(?:await\s+)?(?:vi\.)?importOriginal(?:<[^>]+>)?\s*\(/.test(body)) return true;
  if (/^(?:await\s+)?(?:vi\.)?importActual(?:<[^>]+>)?\s*\(/.test(body)) return true;
  return /^[A-Za-z_$][\w$]*$/.test(body) && aliases.has(body);
}

function objectLiteralDelegates(body: string, aliases: Set<string>): boolean {
  let depth = 0;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (inLineComment) { if (ch === '\n') inLineComment = false; continue; }
    if (inBlockComment) {
      if (ch === '*' && body[i + 1] === '/') { inBlockComment = false; i++; }
      continue;
    }
    if (inString) {
      if (ch === '\\') { i++; continue; }
      if (ch === stringChar) inString = false;
      continue;
    }
    if (ch === '/' && body[i + 1] === '/') { inLineComment = true; i++; continue; }
    if (ch === '/' && body[i + 1] === '*') { inBlockComment = true; i++; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { inString = true; stringChar = ch; continue; }
    if (ch === '{') { depth++; continue; }
    if (ch === '}') { depth--; if (depth <= 0) break; continue; }

    if (depth !== 1 || ch !== '.' || body[i + 1] !== '.' || body[i + 2] !== '.') continue;
    const { expression, nextIndex } = readDelimitedExpression(body, i + 3, new Set([',', '}']));
    if (isDelegatingSource(expression, aliases)) return true;
    i = nextIndex - 1;
  }
  return false;
}

/**
 * True when the factory returns (a spread of) the original module and therefore
 * cannot drift out of sync with the real exports.
 */
export function hasSafeFactoryDelegation(factoryBody: string): boolean {
  const arrowIdx = factoryBody.indexOf('=>');
  if (arrowIdx < 0) return false;

  const i = skipWhitespaceAndComments(factoryBody, arrowIdx + 2);
  if (factoryBody[i] !== '{') {
    const expression = stripWrappingParens(factoryBody.slice(i));
    return isDelegatingSource(expression, new Set()) || (
      expression.startsWith('{') && objectLiteralDelegates(expression, new Set())
    );
  }

  const body = factoryBody.slice(i + 1);
  const aliases = new Set<string>();
  let braceDepth = 1;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let j = 0; j < body.length; j++) {
    const ch = body[j];
    if (inLineComment) { if (ch === '\n') inLineComment = false; continue; }
    if (inBlockComment) {
      if (ch === '*' && body[j + 1] === '/') { inBlockComment = false; j++; }
      continue;
    }
    if (inString) {
      if (ch === '\\') { j++; continue; }
      if (ch === stringChar) inString = false;
      continue;
    }
    if (ch === '/' && body[j + 1] === '/') { inLineComment = true; j++; continue; }
    if (ch === '/' && body[j + 1] === '*') { inBlockComment = true; j++; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { inString = true; stringChar = ch; continue; }

    if (braceDepth === 1 && startsWithWord(body, j, 'return')) {
      const exprStart = skipWhitespaceAndComments(body, j + 'return'.length);
      const { expression, nextIndex } = readDelimitedExpression(body, exprStart, new Set([';', '}']));
      if (isDelegatingSource(expression, aliases)) return true;
      const unwrapped = stripWrappingParens(expression);
      if (unwrapped.startsWith('{') && objectLiteralDelegates(unwrapped, aliases)) return true;
      j = nextIndex - 1;
      continue;
    }

    if (braceDepth === 1 && (startsWithWord(body, j, 'const') || startsWithWord(body, j, 'let') || startsWithWord(body, j, 'var'))) {
      const { expression: statementText, nextIndex } = readDelimitedExpression(body, j, new Set([';']));
      const declMatch = /^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?(?:(?:vi\.)?(?:importOriginal|importActual))\b/.exec(statementText.trim());
      if (declMatch) aliases.add(declMatch[1]);
      j = nextIndex - 1;
      continue;
    }

    if (ch === '{') { braceDepth++; continue; }
    if (ch === '}') { braceDepth--; if (braceDepth <= 0) break; continue; }
  }
  return false;
}

function findReturnedObjectStart(factoryBody: string): number {
  const returnRe = /\breturn\b/g;
  let match: RegExpExecArray | null;
  while ((match = returnRe.exec(factoryBody)) !== null) {
    let i = skipWhitespaceAndComments(factoryBody, match.index + match[0].length);
    if (factoryBody[i] === '(') i = skipWhitespaceAndComments(factoryBody, i + 1);
    if (factoryBody[i] === '{') return i;
  }
  const arrowIdx = factoryBody.indexOf('=>');
  if (arrowIdx >= 0) {
    let i = skipWhitespaceAndComments(factoryBody, arrowIdx + 2);
    if (factoryBody[i] === '(') i = skipWhitespaceAndComments(factoryBody, i + 1);
    if (factoryBody[i] === '{') return i;
  }
  return -1;
}

/** Advance past a balanced bracket/paren/brace section, respecting strings. */
function findFactoryBodyEnd(text: string): number {
  let depth = 0;
  let inString = false;
  let stringChar = '';
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (!inString) {
      if (ch === '(' || ch === '{' || ch === '[') { depth++; }
      else if (ch === ')' || ch === '}' || ch === ']') {
        if (depth === 0) return i; // closing paren of vi.mock(...)
        depth--;
      } else if (ch === '"' || ch === "'" || ch === '`') { inString = true; stringChar = ch; }
      else if (ch === '/' && text[i + 1] === '/') {
        while (i < text.length && text[i] !== '\n') i++;
      } else if (ch === '/' && text[i + 1] === '*') {
        i += 2;
        while (i < text.length - 1 && !(text[i] === '*' && text[i + 1] === '/')) i++;
        i++;
      }
    } else {
      if (ch === '\\') { i++; continue; }
      if (ch === stringChar) inString = false;
    }
  }
  return text.length;
}

/** Extract top-level object property keys from a factory body text. */
export function extractTopLevelKeys(factoryBody: string): Set<string> {
  const keys = new Set<string>();
  const objectStart = findReturnedObjectStart(factoryBody);
  if (objectStart < 0) return keys;

  let depth = 0;
  let expectingKey = false;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let i = objectStart; i < factoryBody.length; i++) {
    const ch = factoryBody[i];
    if (inLineComment) { if (ch === '\n') inLineComment = false; continue; }
    if (inBlockComment) {
      if (ch === '*' && factoryBody[i + 1] === '/') { inBlockComment = false; i++; }
      continue;
    }
    if (inString) {
      if (ch === '\\') { i++; continue; }
      if (ch === stringChar) inString = false;
      continue;
    }
    if (ch === '/' && factoryBody[i + 1] === '/') { inLineComment = true; i++; continue; }
    if (ch === '/' && factoryBody[i + 1] === '*') { inBlockComment = true; i++; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { inString = true; stringChar = ch; continue; }

    if (ch === '{') { depth++; if (depth === 1) expectingKey = true; continue; }
    if (ch === '}') {
      depth--;
      if (depth <= 0) break;
      if (depth === 1) expectingKey = false;
      continue;
    }

    if (depth !== 1) continue;
    if (ch === ',') { expectingKey = true; continue; }
    if (!expectingKey) continue;
    if (!/[A-Za-z_$]/.test(ch)) continue;

    const keyStart = i;
    i++;
    while (i < factoryBody.length && /[A-Za-z0-9_$]/.test(factoryBody[i] ?? '')) i++;
    const key = factoryBody.slice(keyStart, i);
    const next = skipWhitespaceAndComments(factoryBody, i);
    if (factoryBody[next] === ':' || factoryBody[next] === ',' || factoryBody[next] === '}' || factoryBody[next] === '(') {
      keys.add(key);
      i = next - 1;
      expectingKey = factoryBody[next] === ',';
    }
  }
  return keys;
}

// ---------------------------------------------------------------------------
// Inline mock scanner
// ---------------------------------------------------------------------------

interface InlineMockFinding {
  file: string;
  moduleSpec: string;
  resolvedPath: string;
  factoryKeys: Set<string>;
}

/** Find full-replacement vi.mock() factories of real internal modules. */
export function findInternalFullReplacementMocks(filePath: string): InlineMockFinding[] {
  let src: string;
  try { src = readFileSync(filePath, 'utf-8'); } catch { return []; }

  const findings: InlineMockFinding[] = [];
  // Only two-argument vi.mock('<spec>', factory) forms can drift; a bare
  // vi.mock('<spec>') (auto-mock) has no factory to check.
  const mockRe = /vi\.mock\(\s*['"]([^'"]+)['"]\s*,/g;
  let m: RegExpExecArray | null;

  while ((m = mockRe.exec(src)) !== null) {
    const moduleSpec = m[1];
    const resolvedPath = resolveInternalModule(filePath, moduleSpec);
    if (!resolvedPath) continue; // node builtin / bare dep / external — legitimately partial

    const afterComma = src.slice(m.index + m[0].length);
    const endIdx = findFactoryBodyEnd(afterComma);
    const factoryBody = afterComma.slice(0, endIdx);

    if (hasSafeFactoryDelegation(factoryBody)) continue; // spreads original — cannot drift

    const factoryKeys = extractTopLevelKeys(factoryBody);
    findings.push({ file: filePath, moduleSpec, resolvedPath, factoryKeys });
  }
  return findings;
}

/**
 * Core drift comparison: a real export is a NEW drift only when the factory
 * omits it AND it is not grandfathered in the baseline. Pure + unit-testable.
 */
export function computeNewlyMissingExports(
  realExports: Set<string>,
  factoryKeys: Set<string>,
  baselineAllowed: Set<string>,
): string[] {
  return [...realExports].filter(name => !factoryKeys.has(name) && !baselineAllowed.has(name));
}

// ---------------------------------------------------------------------------
// Grandfathering baseline: relTestFile -> moduleSpec -> currently-omitted exports
// Loaded from the sibling JSON data file. Regenerate with
// DUMP_MOCK_COVERAGE_BASELINE=1 (see file header).
// ---------------------------------------------------------------------------

const BASELINE_PATH = path.join(SRC_DIR, 'mock-factory-export-coverage.baseline.json');

function loadBaseline(): Map<string, Map<string, string[]>> {
  const raw = JSON.parse(readFileSync(BASELINE_PATH, 'utf-8')) as Record<string, unknown>;
  const baseline = new Map<string, Map<string, string[]>>();
  for (const [key, value] of Object.entries(raw)) {
    if (key === '_readme') continue; // documentation-only key
    const inner = new Map<string, string[]>();
    for (const [spec, exports] of Object.entries(value as Record<string, string[]>)) {
      inner.set(spec, exports);
    }
    baseline.set(key, inner);
  }
  return baseline;
}

const BASELINE = loadBaseline();

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

// This scan walks every agent test file and dynamically imports each mocked
// runtime module, so its wall-clock is dominated by Vite transform cost, not by
// the assertions. On a constrained VM with a cold cache (./check runs `npm ci`
// first, which wipes node_modules/.vite) that routinely exceeds Vitest's 5s
// default and the test is killed mid-scan — a false red on a correct test.
// Same convention as handlers/bake-smoke-run.test.ts:63.
describe('mock-factory-export-coverage (agent)', { timeout: 120_000 }, () => {
  it('all_full_replacement_mock_factories_cover_runtime_exports', async () => {
    const testFiles = walkTestFiles(SRC_DIR);

    // Sanity: the walker must find a meaningful number of agent test files.
    expect(testFiles.length).toBeGreaterThan(10);

    const failures: string[] = [];
    // Live map used to regenerate the baseline (relFile -> spec -> missing[]).
    const liveMissing: Record<string, Record<string, string[]>> = {};
    // Cache real-module exports across test files that mock the same module.
    const exportsCache = new Map<string, Set<string>>();

    for (const file of testFiles) {
      const rel = path.relative(SRC_DIR, file).replaceAll(path.sep, '/');
      for (const finding of findInternalFullReplacementMocks(file)) {
        let realExports = exportsCache.get(finding.resolvedPath);
        if (!realExports) {
          realExports = await loadRuntimeExports(finding.resolvedPath);
          exportsCache.set(finding.resolvedPath, realExports);
        }

        const missingAll = [...realExports].filter(name => !finding.factoryKeys.has(name)).sort();
        if (missingAll.length > 0) {
          (liveMissing[rel] ??= {})[finding.moduleSpec] = missingAll;
        }

        const baselineAllowed = new Set(BASELINE.get(rel)?.get(finding.moduleSpec) ?? []);
        const newlyMissing = computeNewlyMissingExports(realExports, finding.factoryKeys, baselineAllowed);
        if (newlyMissing.length > 0) {
          failures.push(
            `src/${rel} :: vi.mock("${finding.moduleSpec}") is missing real export(s) ` +
            `[${newlyMissing.sort().join(', ')}]. The real module exposes these but the mock ` +
            `factory does not, so a caller relying on them would hit undefined at test time. ` +
            `Add them to the factory, or (if the omission is intentional) regenerate the ` +
            `baseline with DUMP_MOCK_COVERAGE_BASELINE=1.`,
          );
        }
      }
    }

    if (process.env.DUMP_MOCK_COVERAGE_BASELINE) {
      // Regeneration mode: write the current omitted-export map to the path in
      // the env var so it can be reviewed and committed as the new baseline.
      writeFileSync(process.env.DUMP_MOCK_COVERAGE_BASELINE, JSON.stringify(liveMissing, null, 2) + '\n');
      return; // dump run: do not assert
    }

    expect(failures, `\n${failures.join('\n\n')}`).toHaveLength(0);
  });
});

describe('mock-factory-export-coverage (agent) helpers', () => {
  const thisFile = path.join(SRC_DIR, 'mock-factory-export-coverage.test.ts');
  const nestedFile = path.join(SRC_DIR, 'handlers/example.test.ts');

  it('resolveInternalModule maps a relative .js specifier to the real .ts source', () => {
    // './index.js' from a file in src/ resolves to src/index.ts (which exists).
    expect(resolveInternalModule(thisFile, './index.js')).toBe(path.join(SRC_DIR, 'index.ts'));
    // '../index.js' from src/handlers/ resolves up to src/index.ts.
    expect(resolveInternalModule(nestedFile, '../index.js')).toBe(path.join(SRC_DIR, 'index.ts'));
  });

  it('resolveInternalModule skips node builtins and bare dependencies', () => {
    for (const spec of ['fs', 'child_process', 'os', 'path', 'ws', 'url']) {
      expect(resolveInternalModule(thisFile, spec)).toBeNull();
    }
  });

  it('resolveInternalModule returns null for a relative path with no matching source', () => {
    expect(resolveInternalModule(thisFile, './does-not-exist-module.js')).toBeNull();
    // A relative path escaping src/ is not an internal module.
    expect(resolveInternalModule(thisFile, '../../package.js')).toBeNull();
  });

  it('extractTopLevelKeys ignores nested object members', () => {
    const keys = extractTopLevelKeys(`() => ({
      cloneWithRetry: vi.fn(),
      nested: {
        inner: vi.fn(),
      },
      log: vi.fn(),
    })`);
    expect(keys.has('cloneWithRetry')).toBe(true);
    expect(keys.has('nested')).toBe(true);
    expect(keys.has('log')).toBe(true);
    expect(keys.has('inner')).toBe(false);
  });

  it('extractTopLevelKeys does not treat value identifiers as object keys', () => {
    const keys = extractTopLevelKeys(`() => ({
      cloneWithRetry: cloneWithRetryMock,
    })`);
    expect(keys.has('cloneWithRetry')).toBe(true);
    expect(keys.has('cloneWithRetryMock')).toBe(false);
  });

  it('treats importOriginal spread factories as safe (cannot drift)', () => {
    const body = `async (importOriginal) => {
      const actual = await importOriginal<typeof import('../index.js')>();
      return { ...actual, log: vi.fn() };
    }`;
    expect(hasSafeFactoryDelegation(body)).toBe(true);
  });

  it('treats direct importOriginal return as safe', () => {
    const body = `async (importOriginal) => importOriginal()`;
    expect(hasSafeFactoryDelegation(body)).toBe(true);
  });

  it('treats a plain full-replacement factory as unsafe (must be coverage-checked)', () => {
    const body = `() => ({
      cloneWithRetry: vi.fn(),
      log: vi.fn(),
    })`;
    expect(hasSafeFactoryDelegation(body)).toBe(false);
  });

  it('ignores importOriginal mentions hidden in comments/strings', () => {
    const body = `() => {
      // const actual = await importOriginal();
      const hint = "return { ...actual }";
      return { log: vi.fn() };
    }`;
    expect(hasSafeFactoryDelegation(body)).toBe(false);
  });

  it('computeNewlyMissingExports flags a real export absent from the factory', () => {
    const real = new Set(['a', 'b', 'c']);
    const factory = new Set(['a', 'b']);
    // No baseline -> 'c' is newly-missing drift.
    expect(computeNewlyMissingExports(real, factory, new Set())).toEqual(['c']);
  });

  it('computeNewlyMissingExports grandfathers baseline-allowed omissions', () => {
    const real = new Set(['a', 'b', 'c']);
    const factory = new Set(['a']);
    // 'b' and 'c' are allowed by the baseline -> no new drift.
    expect(computeNewlyMissingExports(real, factory, new Set(['b', 'c']))).toEqual([]);
    // Dropping a previously-covered export ('a') surfaces it as new drift.
    expect(computeNewlyMissingExports(real, new Set([]), new Set(['b', 'c']))).toEqual(['a']);
  });

  it('extractRuntimeExportsFromText captures decl and value re-exports but skips types', () => {
    const names = extractRuntimeExportsFromText(path.join(SRC_DIR, 'git-credential.ts'));
    // git-credential.ts declares these as `export function` / `export const`.
    expect(names.has('writeGitTokenFile')).toBe(true);
    expect(names.has('setupGitCredentialHelper')).toBe(true);
    expect(names.has('GIT_TOKEN_REFRESH_MS')).toBe(true);
  });
});
