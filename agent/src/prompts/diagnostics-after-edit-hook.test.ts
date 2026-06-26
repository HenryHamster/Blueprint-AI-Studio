import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import { execSync } from 'child_process';

// These tests assert hardcoded POSIX forward-slash paths that only match what
// path.join() produces on non-Windows platforms (Windows yields backslashes).
const IS_POSIX = process.platform !== 'win32';

vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
  };
});

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(false),
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
  },
}));

describe('diagnostics-after-edit Claude hook writer', () => {
  let writes: Record<string, string> = {};

  beforeEach(() => {
    writes = {};
    vi.clearAllMocks();
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [filePath, content] = args as [string, string];
      writes[filePath] = content;
    });
  });

  // requires POSIX path separators: settings written under path.join() use '/' only on non-win32
  it.skipIf(!IS_POSIX)('writes a PostToolUse hook and a syntactically valid diagnostics script', async () => {
    const {
      DIAGNOSTICS_AFTER_EDIT_HOOK_MATCHER,
      writeDiagnosticsAfterEditHook,
    } = await import('./diagnostics-after-edit-hook.js');

    expect(writeDiagnosticsAfterEditHook('/tmp/workspace')).toBe(true);

    const settingsRaw = writes['/tmp/workspace/.claude/settings.json'];
    expect(settingsRaw).toBeDefined();
    const settings = JSON.parse(settingsRaw) as {
      hooks: { PostToolUse: Array<{ matcher: string; hooks: Array<{ type: string; command: string; timeout: number }> }> };
    };
    expect(settings.hooks.PostToolUse).toHaveLength(1);
    expect(settings.hooks.PostToolUse[0].matcher).toBe(DIAGNOSTICS_AFTER_EDIT_HOOK_MATCHER);
    expect(settings.hooks.PostToolUse[0].hooks[0]).toEqual({
      type: 'command',
      command: '${CLAUDE_PROJECT_DIR}/.claude/hooks/diagnostics-after-edit.sh',
      timeout: 60,
    });

    const script = writes['/tmp/workspace/.claude/hooks/diagnostics-after-edit.sh'];
    expect(script).toContain('npx eslint');
    expect(script).toContain('npx tsc --noEmit');
    expect(script).toContain('python3 -m ruff check --select E,F,W --ignore E501');
    expect(script).toContain('python3 -m ruff format --check');
    expect(script).toContain('"hookEventName": "PostToolUse"');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  // requires POSIX path separators: appendFileSync is called with a path.join() result ('/' only on non-win32)
  it.skipIf(!IS_POSIX)('keeps generated Claude workspace state out of git', async () => {
    const { writeDiagnosticsAfterEditHook } = await import('./diagnostics-after-edit-hook.js');
    writeDiagnosticsAfterEditHook('/tmp/workspace');

    expect(fs.appendFileSync).toHaveBeenCalledWith('/tmp/workspace/.git/info/exclude', '/.claude/\n');
  });
});
