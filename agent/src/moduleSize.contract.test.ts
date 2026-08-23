import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isModuleSizeBreach } from './moduleSizePolicy.js';

const MODULE_SIZE_LIMITS_RELATIVE = 'backend/testing/module_size_limits.json';

function moduleSizeLimitsPath(): string {
  const candidates = [
    import.meta.url.startsWith('file:')
      ? resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', MODULE_SIZE_LIMITS_RELATIVE)
      : null,
    resolve(process.cwd(), '..', MODULE_SIZE_LIMITS_RELATIVE),
    resolve(process.cwd(), MODULE_SIZE_LIMITS_RELATIVE),
  ].filter((p): p is string => Boolean(p));
  const found = candidates.find(p => existsSync(p));
  if (!found) {
    throw new Error(
      `Cannot locate ${MODULE_SIZE_LIMITS_RELATIVE}. Checked: ${candidates.join(', ')}`,
    );
  }
  return found;
}

function agentSrcRoot(): string {
  const jsonPath = moduleSizeLimitsPath();
  const repoRoot = resolve(dirname(jsonPath), '..', '..');
  const candidates = [
    resolve(repoRoot, 'agent', 'src'),
    import.meta.url.startsWith('file:')
      ? resolve(dirname(fileURLToPath(import.meta.url)))
      : null,
    resolve(process.cwd(), 'src'),
    resolve(process.cwd()),
  ].filter((p): p is string => Boolean(p));
  const found = candidates.find(p => existsSync(p));
  if (!found) {
    throw new Error(`Cannot locate agent/src root. Checked: ${candidates.join(', ')}`);
  }
  return found;
}

/**
 * Count lines using the same semantics as Python's len(f.readlines()):
 * split on '\n', subtract 1 when the file ends with a newline (trailing empty
 * element). A file with no trailing newline counts all elements as-is.
 */
function countLines(content: string): number {
  const parts = content.split('\n');
  return parts[parts.length - 1] === '' ? parts.length - 1 : parts.length;
}

function isAgentExcluded(relPath: string): boolean {
  return relPath.endsWith('.test.ts') || relPath.endsWith('.d.ts');
}

const COVERAGE_THRESHOLD = 1500;

const limitsPath = moduleSizeLimitsPath();
const limits = JSON.parse(readFileSync(limitsPath, 'utf-8')) as {
  agent: Record<string, number>;
};
const agentCaps = limits.agent;

const srcRoot = agentSrcRoot();

describe('agent section: module_size_limits.json cap enforcement', () => {
  it('every capped file exists on disk', () => {
    const missing: string[] = [];
    for (const relPath of Object.keys(agentCaps)) {
      const abs = join(srcRoot, relPath);
      if (!existsSync(abs)) {
        missing.push(relPath);
      }
    }
    expect(
      missing,
      `Some files listed in the agent section of module_size_limits.json do not exist:\n` +
        missing.map(p => `  - ${p}`).join('\n'),
    ).toHaveLength(0);
  });

  for (const [relPath, cap] of Object.entries(agentCaps)) {
    it(`${relPath} is within its cap (module_size_limits.json)`, () => {
      const abs = join(srcRoot, relPath);
      if (!existsSync(abs)) {
        // Existence is checked in the combined test above; skip here to avoid
        // a confusing ENOENT crash on top of the existing failure.
        return;
      }
      const content = readFileSync(abs, 'utf-8');
      const count = countLines(content);
      expect(
        isModuleSizeBreach(count, cap),
        `${relPath} has ${count} lines (limit: ${cap}).\n\n` +
          `This file exceeded its cap in backend/testing/module_size_limits.json.\n` +
          `If your change legitimately grew this file, run:\n` +
          `  python3 backend/testing/check_size_limits.py --fix\n` +
          `to bump the cap via the canonical policy, then commit the JSON edit.\n` +
          `Do NOT refactor or extract code in this PR.`,
      ).toBe(false);
    });
  }
});

describe('agent section: no uncapped large files (module_size_limits.json)', () => {
  it(`no .ts file over ${COVERAGE_THRESHOLD} lines is missing a cap entry`, () => {
    const offenders: Array<{ rel: string; count: number }> = [];

    function walk(dir: string): void {
      for (const entry of readdirSync(dir)) {
        const abs = join(dir, entry);
        const st = statSync(abs);
        if (st.isDirectory()) {
          walk(abs);
        } else if (entry.endsWith('.ts')) {
          const rel = relative(srcRoot, abs).replace(/\\/g, '/');
          if (isAgentExcluded(rel)) continue;
          if (rel in agentCaps) continue;
          const content = readFileSync(abs, 'utf-8');
          const count = countLines(content);
          if (isModuleSizeBreach(count, COVERAGE_THRESHOLD)) {
            offenders.push({ rel, count });
          }
        }
      }
    }

    walk(srcRoot);
    offenders.sort((a, b) => b.count - a.count);

    const detail = offenders
      .map(o => `  - ${o.rel} has ${o.count} lines (threshold: ${COVERAGE_THRESHOLD})`)
      .join('\n');

    expect(
      offenders,
      `${offenders.length} source file(s) exceed ${COVERAGE_THRESHOLD} lines ` +
        `with NO cap entry under 'agent' in backend/testing/module_size_limits.json:\n\n` +
        `${detail}\n\n` +
        `A large file with no cap is invisible to the size guardrails. ` +
        `Run: python3 backend/testing/check_size_limits.py --fix\n` +
        `to add the cap entry, then commit the JSON edit.`,
    ).toHaveLength(0);
  });
});

describe('agent module-size comparison contract', () => {
  it('uses strict greater-than and treats missing values as clean', () => {
    expect(isModuleSizeBreach(1201, 1200)).toBe(true);
    expect(isModuleSizeBreach(1200, 1200)).toBe(false);
    expect(isModuleSizeBreach(null, 1200)).toBe(false);
    expect(isModuleSizeBreach(1201, null)).toBe(false);
  });
});
