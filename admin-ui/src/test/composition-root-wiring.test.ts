import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Composition-root wiring guard.
//
// useWorkbenchCommands declares its strategic-session inputs as OPTIONAL props
// with silent defaults (`markStrategicChecked`, `updateStrategicSessionRefs`,
// `checkingInStrategicSession = false`, ...). Each one is a capability gate:
// when it is absent the hook does not fail, it quietly *removes* the command
// from the palette or turns the action into a no-op. `session.check_in` and
// `session.clear_selected_refs` are filtered out by
// `Boolean(markStrategicChecked)` / `Boolean(updateStrategicSessionRefs)`;
// the keep/remove-context commands are never emitted; the mode commands are
// dropped when `switchStrategicInteractionMode` is missing.
//
// Every one of those props is unit-tested in useWorkbenchCommands.test.tsx by
// injecting it directly, so the whole suite stays green while App.tsx passes
// none of them and the feature is dead in the shipped app. The only thing that
// can catch that is an assertion about the *composition root* itself, which is
// what this file does.
//
// Mirrors the allowlist discipline in orphan-component-guard.test.ts.

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const APP = readFileSync(path.join(SRC_DIR, 'App.tsx'), 'utf-8');
const WORKBENCH_COMMANDS = readFileSync(
  path.join(SRC_DIR, 'hooks', 'useWorkbenchCommands.ts'),
  'utf-8',
);

// Props that are deliberately NOT supplied by App.tsx. Add an entry here with a
// one-line rationale rather than deleting the assertion.
const UNWIRED_ALLOWLIST = new Set<string>([]);

/** Brace-matched slice starting at the first `{` at or after `from`. */
function objectLiteralAt(source: string, from: number): string {
  const start = source.indexOf('{', from);
  if (start < 0) throw new Error('no object literal found');
  let depth = 0;
  for (let i = start; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced object literal');
}

/** Top-level (depth-1) keys of an object literal, shorthand entries included. */
function topLevelKeys(literal: string): string[] {
  const keys: string[] = [];
  const body = literal.slice(1, -1);
  let depth = 0;
  let token = '';
  const flush = () => {
    const m = token.trim().match(/([a-zA-Z_$][\w$]*)\s*$/);
    if (m) keys.push(m[1]);
    token = '';
  };
  for (let i = 0; i < body.length; i++) {
    const c = body[i];
    if (c === '{' || c === '(' || c === '[') { depth++; token += c; continue; }
    if (c === '}' || c === ')' || c === ']') { depth--; token += c; continue; }
    if (depth === 0 && c === ',') { flush(); continue; }
    if (depth === 0 && c === ':') {
      flush();
      // Skip the value so nothing inside it is mistaken for a key.
      let valueDepth = 0;
      for (i++; i < body.length; i++) {
        const q = body[i];
        if ('{(['.includes(q)) valueDepth++;
        else if ('})]'.includes(q)) valueDepth--;
        else if (q === ',' && valueDepth === 0) break;
      }
      continue;
    }
    token += c;
  }
  flush();
  return keys;
}

function declaredOptions(): string[] {
  const from = WORKBENCH_COMMANDS.indexOf('interface UseWorkbenchCommandsOptions');
  expect(from, 'UseWorkbenchCommandsOptions interface not found').toBeGreaterThan(-1);
  const body = WORKBENCH_COMMANDS.slice(from, WORKBENCH_COMMANDS.indexOf('\n}', from));
  return [...body.matchAll(/^ {2}([a-zA-Z_$][\w$]*)\??\s*:/gm)].map(m => m[1]);
}

describe('App passes every option useWorkbenchCommands declares', () => {
  it('leaves no declared option to its silent default', () => {
    const call = objectLiteralAt(APP, APP.indexOf('useWorkbenchCommands('));
    const passed = new Set(topLevelKeys(call));
    const declared = declaredOptions();

    // Sanity: the parser actually found the real call, not an empty match.
    expect(declared.length).toBeGreaterThan(30);
    expect(passed.size).toBeGreaterThan(30);

    const missing = declared.filter(
      name => !passed.has(name) && !UNWIRED_ALLOWLIST.has(name),
    );
    expect(
      missing,
      `App.tsx does not pass these useWorkbenchCommands options, so each falls back to its `
        + `silent default and the commands gated on it are dropped from the palette: ${missing.join(', ')}`,
    ).toEqual([]);
  });

  it('supplies the strategic-session capability gates by name', () => {
    const call = objectLiteralAt(APP, APP.indexOf('useWorkbenchCommands('));
    const passed = new Set(topLevelKeys(call));
    // Pinned literally: these are the gates whose absence silently deletes a
    // command instead of failing, so a future refactor must trip this test.
    for (const gate of [
      'markStrategicChecked',
      'updateStrategicSessionRefs',
      'checkingInStrategicSession',
      'savingStrategicWorkbench',
      'switchStrategicInteractionMode',
      'strategicInteractionMode',
      'strategicWorkbenchState',
    ]) {
      expect(passed.has(gate), `App.tsx must pass ${gate} to useWorkbenchCommands`).toBe(true);
    }
  });

  it('routes check-in through the hook callback rather than a fresh closure', () => {
    // An inline arrow in the options object would be a new identity every
    // render and would re-derive the entire memoized command list.
    expect(APP).toContain('const markStrategicChecked = useCallback(');
    expect(APP).toContain('void handleStrategicCheckIn();');
  });
});

describe('App passes the Pipeline rail its session-ref surface', () => {
  it('includes taskContextSessionProps in the rail literal', () => {
    // `rail={(` also appears in App.tsx for a different component; anchor on the
    // object-literal form used by PipelineWorkspace.
    const railIdx = APP.indexOf('rail={{');
    expect(railIdx, 'PipelineWorkspace rail literal not found').toBeGreaterThan(-1);
    const rail = objectLiteralAt(APP, railIdx + 'rail={'.length);
    const keys = topLevelKeys(rail);

    expect(keys.length).toBeGreaterThan(3);
    expect(
      keys,
      'PipelineWorkspace spreads {...rail.taskContextSessionProps} into TaskContextRail; '
        + 'without this key the rail session-ref UI is inert in production.',
    ).toContain('taskContextSessionProps');
  });

  it('takes taskContextSessionProps off the strategic session hook', () => {
    expect(APP).toContain('taskContextSessionProps');
    const destructure = APP.slice(APP.indexOf('} = useStrategicSession({') - 900, APP.indexOf('} = useStrategicSession({'));
    for (const value of [
      'taskContextSessionProps',
      'updateStrategicSessionRefs',
      'checkingInStrategicSession',
      'handleStrategicCheckIn',
    ]) {
      expect(destructure, `App.tsx must destructure ${value} from useStrategicSession`).toContain(value);
    }
  });
});
