import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest';
import { readFileSync, readdirSync, statSync, mkdtempSync, rmSync, unlinkSync } from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  AGENT_HELPER_NAMES,
  writePlaybookHelper,
  writeMessagesHelper,
  writeDesignDocHelper,
} from './prompt.js';
import { writeStrategicHelpers } from './handlers/strategic-helpers.js';

const SRC_DIR = path.dirname(fileURLToPath(import.meta.url));

// Strip both block and line comments so doc-comment examples (`// ./design-doc`)
// don't read as live prompt advertisements.
function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}


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
    const allowed = new Set(AGENT_HELPER_NAMES);

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

// The drift test above only proves a ./<name> token is globally spelled right.
// It does NOT prove the *specific* handler whose prompt advertises a helper
// actually writes that helper into its own workspace — so a handler can promise
// a helper it never provisions and the agent hits "command not found" when it
// follows the prompt. This per-handler invariant closes that gap: it provisions
// each handler's workspace by invoking the same writer functions the handler
// invokes, builds the advertised-helper set from the handler's prompt source,
// and asserts every advertised (non-ambient) helper is a real executable file.
describe('per-handler prompt/helper provisioning parity', () => {
  // Lifecycle helpers provisioned by the shared code-change path / wrapper for
  // every task, not by the per-handler writers under test here.
  const AMBIENT = new Set(['check', 'pr', 'done', 'fail', 'blocked', 'note']);
  const TIER2 = AGENT_HELPER_NAMES.filter((n) => !AMBIENT.has(n));

  // Maps a writer function name (as it appears at a call site in handler source)
  // to a real invocation that provisions into the given workspace. Executing the
  // genuine writers means a wrong filename or a missing executable bit is caught,
  // not just a missing call.
  const WRITER_INVOKERS: Record<string, (ws: string) => void> = {
    writePlaybookHelper: (ws) => { writePlaybookHelper('t-helpertest', ws); },
    writeMessagesHelper: (ws) => { writeMessagesHelper('t-helpertest', ws); },
    writeDesignDocHelper: (ws) => { writeDesignDocHelper('t-helpertest', ws); },
    writeStrategicHelpers: (ws) => { writeStrategicHelpers('t-helpertest', 'p-helpertest', ws); },
  };

  // Task types whose prompts advertise tier-2 helpers they provision locally.
  const HANDLERS: Array<{ type: string; sources: string[] }> = [
    { type: 'research', sources: ['handlers/research-plan.ts'] },
    { type: 'plan', sources: ['handlers/research-plan.ts'] },
    { type: 'strategic_review', sources: ['handlers/strategic.ts'] },
    { type: 'design_doc_update', sources: ['handlers/design-doc.ts'] },
  ];

  let ws: string;
  beforeAll(() => { ws = mkdtempSync(path.join(os.tmpdir(), 'helper-refs-ws-')); });
  afterAll(() => { rmSync(ws, { recursive: true, force: true }); });
  beforeEach(() => {
    for (const f of readdirSync(ws)) {
      try { unlinkSync(path.join(ws, f)); } catch { /* ignore */ }
    }
  });

  const tokenRe = /\.\/([a-z][a-z-]*)(?![\w/.-])/g;

  for (const h of HANDLERS) {
    it(`${h.type}: every advertised tier-2 helper is provisioned as an executable`, () => {
      const text = h.sources
        .map((s) => stripComments(readFileSync(path.join(SRC_DIR, s), 'utf-8')))
        .join('\n');

      // Provision exactly the writers this handler calls (detected from source).
      const writers = Object.keys(WRITER_INVOKERS).filter((w) =>
        new RegExp(`\\b${w}\\s*\\(`).test(text)
      );
      expect(writers.length, `${h.type}: no helper writers detected in source`).toBeGreaterThan(0);
      for (const w of writers) WRITER_INVOKERS[w](ws);

      const provided = new Set(
        readdirSync(ws).filter((f) => {
          try { return (statSync(path.join(ws, f)).mode & 0o111) !== 0; } catch { return false; }
        })
      );

      // Helpers the prompt advertises (tier-2 only; ambient lifecycle helpers
      // are provisioned elsewhere and exempt).
      const advertised = new Set<string>();
      let m: RegExpExecArray | null;
      tokenRe.lastIndex = 0;
      while ((m = tokenRe.exec(text)) !== null) {
        if (TIER2.includes(m[1])) advertised.add(m[1]);
      }
      expect(advertised.size, `${h.type}: scan found no tier-2 ./<helper> advertisements`).toBeGreaterThan(0);

      const missing = [...advertised].filter((a) => !provided.has(a));
      expect(
        missing,
        `${h.type} prompt advertises ./${missing.join(', ./')} but its workspace ` +
          `provisions only [${[...provided].sort().join(', ')}]. Either provision the ` +
          `helper (call its write*Helper in the handler) or stop advertising it in the prompt.`
      ).toEqual([]);
    });
  }
});
