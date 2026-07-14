import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

// Static-analysis guard for the warm-VM per-role reset invariant.
//
// Every module-level variable tagged `// @per-role` must be cleared between
// warm-VM roles. This file checks:
//   1. All @per-role vars in each source file appear in that module's reset
//      function(s).
//   2. Each module's reset function(s) are called from resetPerRoleModuleState
//      in index.ts.
//
// Adding a `// @per-role` marker without wiring a reset fails these tests.

const dir = dirname(fileURLToPath(import.meta.url));

function readSrc(relPath: string): string {
  return readFileSync(join(dir, relPath), 'utf-8');
}

function extractPerRoleVars(src: string): string[] {
  const vars: string[] = [];
  for (const line of src.split('\n')) {
    if (line.includes('// @per-role')) {
      const m = line.match(/(?:let|const)\s+(\w+)/);
      if (m) vars.push(m[1]);
    }
  }
  return vars;
}

function extractFnBody(src: string, fn: string): string {
  const pat = new RegExp(`(?:export\\s+)?function\\s+${fn}\\s*\\(`);
  const idx = src.search(pat);
  if (idx === -1) return '';
  // Skip past the full parameter list (counting parens) to avoid mistaking
  // default-arg braces like `= {}` for the function body's opening brace.
  const parenStart = src.indexOf('(', idx);
  if (parenStart === -1) return '';
  let pd = 1; let pi = parenStart + 1;
  while (pi < src.length && pd > 0) {
    if (src[pi] === '(') pd++;
    else if (src[pi] === ')') pd--;
    pi++;
  }
  const braceIdx = src.indexOf('{', pi);
  if (braceIdx === -1) return '';
  let depth = 1;
  let i = braceIdx + 1;
  while (i < src.length && depth > 0) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(braceIdx + 1, i - 1);
}

describe('warm-VM per-role reset invariant (external modules)', () => {
  const indexSrc = readSrc('index.ts');
  const resetBody = extractFnBody(indexSrc, 'resetPerRoleModuleState');

  // ── telemetry.ts ────────────────────────────────────────────────────────────

  describe('telemetry.ts', () => {
    const src = readSrc('telemetry.ts');
    const perRoleVars = extractPerRoleVars(src);
    const resetFns = ['resetLlmTelemetry', 'resetFormatComplianceTelemetry', 'setCurrentPromptFingerprint', 'resetResidentTurnLatencyTelemetry', 'resetOperatorDecisionCaptureTelemetry'];

    it('has at least one @per-role variable', () => {
      expect(perRoleVars.length).toBeGreaterThan(0);
    });

    it('every @per-role var appears in its module reset function', () => {
      const combinedResetBodies = resetFns.map(fn => extractFnBody(src, fn)).join('\n');
      for (const v of perRoleVars) {
        expect(
          combinedResetBodies,
          `@per-role var "${v}" in telemetry.ts not found in any of: ${resetFns.join(', ')}`,
        ).toMatch(new RegExp(`\\b${v}\\b`));
      }
    });

    it('module reset functions are all called from resetPerRoleModuleState', () => {
      for (const fn of resetFns) {
        expect(
          resetBody,
          `${fn} not called from resetPerRoleModuleState`,
        ).toContain(`${fn}(`);
      }
    });
  });

  // ── workspacePrep.ts ─────────────────────────────────────────────────────────

  describe('workspacePrep.ts', () => {
    const src = readSrc('workspacePrep.ts');
    const perRoleVars = extractPerRoleVars(src);
    const resetFn = 'resetWorkspacePrepOutcome';

    it('has at least one @per-role variable', () => {
      expect(perRoleVars.length).toBeGreaterThan(0);
    });

    it('every @per-role var appears in resetWorkspacePrepOutcome', () => {
      const body = extractFnBody(src, resetFn);
      for (const v of perRoleVars) {
        expect(
          body,
          `@per-role var "${v}" in workspacePrep.ts not found in ${resetFn}`,
        ).toMatch(new RegExp(`\\b${v}\\b`));
      }
    });

    it('resetWorkspacePrepOutcome is called from resetPerRoleModuleState', () => {
      expect(resetBody, `${resetFn} not called from resetPerRoleModuleState`).toContain(`${resetFn}(`);
    });
  });

  // ── prompts/discovery.ts ─────────────────────────────────────────────────────

  describe('prompts/discovery.ts', () => {
    const src = readSrc('prompts/discovery.ts');
    const perRoleVars = extractPerRoleVars(src);
    const resetFn = 'clearTestConventionCache';

    it('has at least one @per-role variable', () => {
      expect(perRoleVars.length).toBeGreaterThan(0);
    });

    it('every @per-role var appears in clearTestConventionCache', () => {
      const body = extractFnBody(src, resetFn);
      for (const v of perRoleVars) {
        expect(
          body,
          `@per-role var "${v}" in prompts/discovery.ts not found in ${resetFn}`,
        ).toMatch(new RegExp(`\\b${v}\\b`));
      }
    });

    it('clearTestConventionCache is called from resetPerRoleModuleState', () => {
      expect(resetBody, `${resetFn} not called from resetPerRoleModuleState`).toContain(`${resetFn}(`);
    });
  });
});
