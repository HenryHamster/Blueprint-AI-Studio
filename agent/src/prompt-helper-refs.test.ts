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
import * as promptModule from './prompt.js';
import {
  writeStrategicHelpers,
  writeFetchHelper,
  DESIGN_DOC_FETCH_VERBS,
} from './handlers/strategic-helpers.js';
import { provisionAmbientHelpers } from './providers/runners.js';

const SRC_DIR = path.dirname(fileURLToPath(import.meta.url));

// These tests assert helper scripts are written with the Unix executable bit
// (statSync mode & 0o111). Windows has no executable bit, so probe the real
// capability and skip there while still running fully in Linux CI.
const SUPPORTS_EXEC_BIT = process.platform !== 'win32';

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
// Requires the Unix executable bit (statSync mode & 0o111) — skip where absent.
describe.skipIf(!SUPPORTS_EXEC_BIT)('per-handler prompt/helper provisioning parity', () => {
  // ── Helper categories ──────────────────────────────────────────────────────
  // AMBIENT helpers are provisioned for EVERY model task by the provider runner
  // (provisionAmbientHelpers, called from runProviderAgent). The test exercises
  // that EXACT function, so a regression in the runtime's ambient provisioning
  // is caught here rather than silently exempted.
  const AMBIENT = new Set(['note', 'playbook', 'messages', 'symbols']);

  // LIFECYCLE helpers are provisioned by the code-change wrapper
  // (writeCodeChangeHelpers) for code-change tasks — neither ambient nor written
  // by these read-only handlers. A read-only handler that references one in
  // passing is not a contract breach, so they are exempt from the on-disk
  // requirement here. (The global allowlist test above still pins their spelling.)
  const LIFECYCLE_EXEMPT = new Set([
    'check', 'pr', 'done', 'fail', 'blocked', 'compute-image',
  ]);

  // Collect the ./<helper> tokens (restricted to real helper names) in a blob.
  const helperTokensIn = (text: string): Set<string> => {
    const re = /\.\/([a-z][a-z-]*)(?![\w/.-])/g;
    const out = new Set<string>();
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      if (AGENT_HELPER_NAMES.includes(m[1])) out.add(m[1]);
    }
    return out;
  };

  // String-valued exports from prompt.ts that advertise a ./helper (e.g.
  // DEFAULT_TOOL_GUIDANCE -> ./note,./messages; PLAYBOOK_GUIDANCE -> ./playbook).
  // A handler that interpolates one (`${DEFAULT_TOOL_GUIDANCE}`) advertises the
  // helpers inside it exactly as if the text were inline — web_research, design,
  // and memo reference their helpers ONLY this way — so the scan must expand them.
  const HELPER_BEARING_CONSTANTS: Record<string, string> = {};
  for (const [name, val] of Object.entries(promptModule)) {
    if (typeof val === 'string' && helperTokensIn(val).size > 0) {
      HELPER_BEARING_CONSTANTS[name] = val;
    }
  }

  // Maps a writer function name (as it appears at a call site in handler source)
  // to a real invocation that provisions into the given workspace. Executing the
  // genuine writers means a wrong filename or a missing executable bit is caught,
  // not just a missing call.
  const WRITER_INVOKERS: Record<string, (ws: string) => void> = {
    writePlaybookHelper: (ws) => { writePlaybookHelper('t-helpertest', ws); },
    writeMessagesHelper: (ws) => { writeMessagesHelper('t-helpertest', ws); },
    writeDesignDocHelper: (ws) => { writeDesignDocHelper('t-helpertest', ws); },
    writeStrategicHelpers: (ws) => { writeStrategicHelpers('t-helpertest', 'p-helpertest', ws); },
    writeFetchHelper: (ws) => { writeFetchHelper('t-helpertest', 'p-helpertest', ws, { verbs: DESIGN_DOC_FETCH_VERBS }); },
  };

  // Every model-prompt-emitting handler whose prompt advertises helpers. The
  // three read-only research-family handlers (web_research, design, memo) — which
  // rely entirely on ambient provisioning — are covered alongside the four that
  // also provision tier-2 helpers locally.
  const HANDLERS: Array<{ type: string; sources: string[] }> = [
    { type: 'web_research', sources: ['handlers/web-research.ts'] },
    { type: 'design', sources: ['handlers/design.ts'] },
    { type: 'memo', sources: ['handlers/memo.ts'] },
    { type: 'research', sources: ['handlers/research-plan.ts'] },
    { type: 'plan', sources: ['handlers/research-plan.ts'] },
    { type: 'strategic_review', sources: ['handlers/strategic.ts'] },
    { type: 'design_doc_update', sources: ['handlers/design-doc.ts'] },
    { type: 'create', sources: ['handlers/standard.ts'] },
    { type: 'refactor', sources: ['handlers/standard.ts'] },
    { type: 'review', sources: ['handlers/review-fix.ts'] },
    { type: 'fix', sources: ['handlers/review-fix.ts'] },
    { type: 'migrate', sources: ['handlers/specialized.ts'] },
    { type: 'debug', sources: ['handlers/specialized.ts'] },
    { type: 'perf', sources: ['handlers/specialized.ts'] },
    { type: 'investigate', sources: ['handlers/specialized.ts'] },
    { type: 'smoketest', sources: ['handlers/specialized.ts'] },
    { type: 'security_audit', sources: ['handlers/audits.ts'] },
    { type: 'self_audit', sources: ['handlers/audits.ts'] },
    { type: 'release_audit', sources: ['handlers/audits.ts'] },
  ];

  let ws: string;
  beforeAll(() => { ws = mkdtempSync(path.join(os.tmpdir(), 'helper-refs-ws-')); });
  afterAll(() => { rmSync(ws, { recursive: true, force: true }); });
  beforeEach(() => {
    for (const f of readdirSync(ws)) {
      try { unlinkSync(path.join(ws, f)); } catch { /* ignore */ }
    }
  });

  const executablesIn = (dir: string): Set<string> =>
    new Set(
      readdirSync(dir).filter((f) => {
        try { return (statSync(path.join(dir, f)).mode & 0o111) !== 0; } catch { return false; }
      })
    );

  // Pin the ambient provisioner's own contract: exercising the EXACT function the
  // runtime calls must yield ./note, ./playbook, ./messages as real executables.
  // If this regresses, every handler that relies on ambient helpers is broken.
  it('provisionAmbientHelpers writes ./note, ./playbook, ./messages, ./symbols as executables', () => {
    provisionAmbientHelpers('t-helpertest', ws);
    const provided = executablesIn(ws);
    for (const name of AMBIENT) {
      expect(provided.has(name), `provisionAmbientHelpers must write an executable ./${name}`).toBe(true);
    }
  });

  for (const h of HANDLERS) {
    it(`${h.type}: every advertised helper is provisioned as an executable`, () => {
      const handlerSrc = h.sources
        .map((s) => stripComments(readFileSync(path.join(SRC_DIR, s), 'utf-8')))
        .join('\n');

      // The handler's EFFECTIVE prompt text = its own source plus the bodies of
      // any helper-bearing guidance constants it interpolates. Without this
      // expansion web_research/design/memo would appear to advertise nothing
      // (their ./playbook + ./messages live in ${PLAYBOOK_GUIDANCE} /
      // ${DEFAULT_TOOL_GUIDANCE}, not in literal handler source).
      let effectiveText = handlerSrc;
      for (const [name, val] of Object.entries(HELPER_BEARING_CONSTANTS)) {
        if (new RegExp(`\\b${name}\\b`).test(handlerSrc)) effectiveText += '\n' + val;
      }

      // (1) Ambient provisioning — the EXACT path runProviderAgent runs for every
      // model task. Guarantees ./note, ./playbook, ./messages on disk.
      provisionAmbientHelpers('t-helpertest', ws);

      // (2) Handler-specific tier-2 writers detected from the handler's own
      // source, exercised through their real generators (./design-doc, ./strategic,
      // ./fetch). A handler with no tier-2 writers (web_research, memo) provisions
      // nothing extra and relies entirely on ambient helpers — which is correct.
      const writers = Object.keys(WRITER_INVOKERS).filter((w) =>
        new RegExp(`\\b${w}\\s*\\(`).test(handlerSrc)
      );
      for (const w of writers) WRITER_INVOKERS[w](ws);

      const provided = executablesIn(ws);

      // Helpers the prompt advertises, minus the wrapper-provisioned lifecycle
      // helpers (exempt here — see LIFECYCLE_EXEMPT).
      const advertised = new Set(
        [...helperTokensIn(effectiveText)].filter((n) => !LIFECYCLE_EXEMPT.has(n))
      );
      expect(advertised.size, `${h.type}: scan found no ./<helper> advertisements`).toBeGreaterThan(0);

      const missing = [...advertised].filter((a) => !provided.has(a));
      expect(
        missing,
        `${h.type} prompt advertises ./${missing.join(', ./')} but its workspace ` +
          `provisions only [${[...provided].sort().join(', ')}]. Provision the helper — ` +
          `ambiently via provisionAmbientHelpers (./note, ./playbook, ./messages) or by ` +
          `calling its write*Helper in the handler — or stop advertising it in the prompt.`
      ).toEqual([]);
    });
  }
});

// The per-handler helper-provisioning test above guarantees a handler provisions
// every helper its prompt advertises. This complementary parity guard makes the
// SHARED LAUNCH-CONTEXT block a structural guarantee the same way: every
// model-prompt-emitting code/analysis handler must call buildContextPrefix so it
// surfaces sibling/queued task awareness, the served direction, and quality
// signals to the agent. Without this, equipping is a per-handler choice — which
// is exactly how investigate launched with none while its near-twin debug
// launched fully equipped.
describe('per-handler launch-context parity', () => {
  // Extract a top-level `export async function <name>(...) { ... }` body by
  // brace-balancing from the signature's opening brace. String/comment-aware
  // enough for this source (no `{`/`}` inside string literals in these bodies).
  const extractFnBody = (src: string, name: string): string | null => {
    const sig = new RegExp(`export\\s+async\\s+function\\s+${name}\\s*\\(`).exec(src);
    if (!sig) return null;
    // Skip past the parameter list (which itself contains braces, e.g.
    // `statusRef: { value: string }`) by paren-balancing to the signature's
    // closing `)`, THEN take the next `{` as the function body's open brace.
    const parenStart = src.indexOf('(', sig.index);
    let pd = 0;
    let parenEnd = -1;
    for (let i = parenStart; i < src.length; i++) {
      if (src[i] === '(') pd++;
      else if (src[i] === ')') { pd--; if (pd === 0) { parenEnd = i; break; } }
    }
    if (parenEnd < 0) return null;
    const braceStart = src.indexOf('{', parenEnd);
    if (braceStart < 0) return null;
    let depth = 0;
    for (let i = braceStart; i < src.length; i++) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') {
        depth--;
        if (depth === 0) return src.slice(braceStart, i + 1);
      }
    }
    return null;
  };

  // Enumerate EVERY exported `handle*` function in specialized.ts dynamically, so
  // a newly-added specialized handler that omits the launch-context block fails
  // this test without anyone having to remember to register it here.
  const specializedSrc = readFileSync(path.join(SRC_DIR, 'handlers/specialized.ts'), 'utf-8');
  const handlerNames = [
    ...specializedSrc.matchAll(/export\s+async\s+function\s+(handle[A-Za-z0-9_]+)\s*\(/g),
  ].map((m) => m[1]);

  it('specialized.ts exposes the expected model-prompt-emitting handlers', () => {
    // Guard against a regex/refactor that silently turns the sweep into a no-op.
    expect(handlerNames).toEqual(
      expect.arrayContaining([
        'handleMigrate', 'handleSmoketest', 'handleDebug', 'handlePerf', 'handleInvestigate',
      ]),
    );
  });

  for (const name of handlerNames) {
    it(`${name} includes the shared launch-context block (buildContextPrefix)`, () => {
      const body = extractFnBody(specializedSrc, name);
      expect(body, `could not extract body of ${name}`).not.toBeNull();
      expect(
        (body ?? '').includes('buildContextPrefix('),
        `${name} builds a model prompt without the shared launch-context block. ` +
          `Call buildContextPrefix(task) and render it in the prompt (it degrades ` +
          `to '' when context is absent), so this handler launches as well-equipped ` +
          `as its peers (create/fix/refactor/debug).`,
      ).toBe(true);
    });
  }
});
