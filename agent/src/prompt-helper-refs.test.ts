import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { AGENT_HELPER_NAMES } from './prompt.js';

const SRC_DIR = path.dirname(fileURLToPath(import.meta.url));

// `./fetch` is a real helper script written by handlers/research-plan.ts
// (see writeFileSync of the fetch helper at runtime), but it is NOT registered
// in AGENT_HELPER_NAMES. Tracked in the playbook (prompt-drift-inventory).
// Keep this allow-list tiny — it should shrink, not grow.
const KNOWN_EXTRA_HELPERS = new Set(['fetch']);

function listTsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...listTsFiles(full));
    else if (entry.endsWith('.ts') && !entry.endsWith('.test.ts')) out.push(full);
  }
  return out;
}

describe('helper-script reference drift', () => {
  it('every ./<name> token in prompt-emitting source matches AGENT_HELPER_NAMES', () => {
    const files = [
      ...listTsFiles(path.join(SRC_DIR, 'prompts')),
      ...listTsFiles(path.join(SRC_DIR, 'handlers')),
      path.join(SRC_DIR, 'prompt.ts'),
    ];

    // Require non-word, non-slash, non-dot, non-hyphen boundary after the
    // word — excludes `./tmp/...`, `./foo.js`, `./check-something`.
    const tokenRe = /\.\/([a-z][a-z-]*)(?![\w/.-])/g;
    const allowed = new Set([...AGENT_HELPER_NAMES, ...KNOWN_EXTRA_HELPERS]);

    const found = new Set<string>();
    const offenders: string[] = [];

    for (const file of files) {
      const raw = readFileSync(file, 'utf-8');
      // Strip line comments so doc-comment examples like `// './module-name'`
      // in discovery.ts don't pollute the scan.
      const text = raw.replace(/\/\/.*$/gm, '');
      let m: RegExpExecArray | null;
      while ((m = tokenRe.exec(text)) !== null) {
        const tok = m[1];
        found.add(tok);
        if (!allowed.has(tok)) {
          offenders.push(`${path.relative(SRC_DIR, file)}: ./${tok}`);
        }
      }
    }

    // Guard against regex bugs that turn the test into a no-op.
    expect(found.size).toBeGreaterThan(3);

    if (offenders.length > 0) {
      const list = [...new Set(offenders)].sort().join('\n  ');
      const allowList = [...allowed].sort().join(', ');
      throw new Error(
        `Found ./<name> reference(s) not in AGENT_HELPER_NAMES.\n` +
          `  ${list}\n` +
          `Current allowlist: ${allowList}\n` +
          `Either add the helper to AGENT_HELPER_NAMES in agent/src/prompt.ts ` +
          `or change the prompt to reference an existing helper.`
      );
    }
  });
});
