import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  writeStrategicHelpers,
  writeFetchHelper,
  buildFetchGuidance,
  RESEARCH_FETCH_VERBS,
  PLAN_FETCH_VERBS,
  DESIGN_DOC_FETCH_VERBS,
  STRATEGIC_FETCH_VERBS,
} from './strategic-helpers.js';

// Use the OS temp dir (not a hardcoded /tmp) so this suite runs on Windows too.
const testWorkspace = path.join(os.tmpdir(), 'test-strategic-helpers-workspace');

describe('writeStrategicHelpers', () => {
  beforeEach(() => {
    if (!fs.existsSync(testWorkspace)) {
      fs.mkdirSync(testWorkspace, { recursive: true });
    }
  });

  afterEach(() => {
    // Clean up test workspace
    if (fs.existsSync(testWorkspace)) {
      try {
        fs.rmSync(testWorkspace, { recursive: true });
      } catch { /* ignore */ }
    }
  });

  it('writes three helper scripts to workspace', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    expect(fs.existsSync(`${testWorkspace}/strategic`)).toBe(true);
    expect(fs.existsSync(`${testWorkspace}/fetch`)).toBe(true);
    expect(fs.existsSync(`${testWorkspace}/design-doc`)).toBe(true);
  });

  it('writes scripts with executable permissions', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const stat = fs.statSync(`${testWorkspace}/strategic`);
    // POSIX execute bits are not modeled on Windows (mode & 0o111 is always 0
    // there regardless of the requested 0o755), so the bit assertion only makes
    // sense on POSIX. On Windows just assert the file was created.
    if (process.platform === 'win32') {
      expect(stat.isFile()).toBe(true);
    } else {
      expect((stat.mode & 0o111) !== 0).toBe(true); // Check execute bit
    }
  });

  it('embeds taskId in strategic script safely', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    expect(content).toContain(`TASK="${taskId}"`);
    expect(content).toContain('set-problem-model');
    expect(content).toContain('create-direction');
    expect(content).toContain('set-running-update');
  });

  it('embeds projectId in fetch script safely', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(content).toContain(`PROJECT="${projectId}"`);
    expect(content).toContain('problem-model');
    expect(content).toContain('direction');
    expect(content).toContain('playbook');
    expect(content).toContain('gap');
    expect(content).toContain('message');
  });

  it('supports fetching a message by short ref via the messages endpoint', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // The new subcommand maps `fetch message <ref>` to the read-only lookup
    // endpoint that resolves a short ref or full UUID backend-side.
    expect(content).toContain('message)    PATH_SUFFIX="messages/$ID"');
    expect(content).toContain('fetch problem-model|direction|playbook|gap|message|section|knowledge [id]');
    // Reuses the existing X-Agent-Token GET path — no new auth/curl block.
    expect(content).toContain('-H "X-Agent-Token: $TOKEN"');
    // The shared id-required guard covers `message` (only problem-model is id-less).
    expect(content).toContain('fetch $KIND requires an id argument');
  });

  it('allows fetching the full problem model without an id', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(content).toContain('Usage: fetch problem-model|direction|playbook|gap|message|section|knowledge [id]');
    expect(content).toContain('problem-model) PATH_SUFFIX="problem-model"');
    expect(content).toContain('fetch $KIND requires an id argument');
  });

  it('maps `fetch section <key>` to the read-only design-doc endpoint', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // `fetch section systems/auth` -> GET .../design-doc/systems/auth (key may
    // contain a slash; the backend route is `{key:path}`).
    expect(content).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
    // Section requires an id (key) — covered by the shared id-required guard.
    expect(content).toContain('fetch $KIND requires an id argument');
  });

  it('embeds taskId in design-doc script safely', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/design-doc`, 'utf-8');
    // Shared durable generator (prompt.ts) bakes the task id into a TASK var and
    // interpolates it via printf, rather than inlining it in each curl body.
    expect(content).toContain(`TASK="${taskId}"`);
    expect(content).toContain('get|set|delete|list');
  });

  it('prevents shell injection in taskId with special characters', () => {
    const taskId = 'test$(whoami)task';
    const projectId = 'project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const strategicContent = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    // The `$` is backslash-escaped before being baked into the double-quoted
    // TASK assignment, so bash sees a literal string, not a command substitution.
    expect(strategicContent).toContain('TASK="test\\$(whoami)task"');
    // The dangerous *unescaped* command-substitution form must NOT appear — that
    // is the form that would actually run `whoami` at script load time. (We can't
    // just assert the absence of the substring "$(whoami)" because the safe,
    // escaped form "\$(whoami)" legitimately contains it.)
    expect(strategicContent).not.toContain('TASK="test$(whoami)task"');
    // Every "$(whoami)" occurrence must be backslash-escaped.
    expect(/(^|[^\\])\$\(whoami\)/.test(strategicContent)).toBe(false);
  });

  it('prevents shell injection in projectId with special characters', () => {
    const taskId = 'test-task';
    const projectId = 'project$(echo hacked)id';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const fetchContent = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // The `$` is backslash-escaped so the substitution never executes.
    expect(fetchContent).toContain('PROJECT="project\\$(echo hacked)id"');
    expect(fetchContent).not.toContain('PROJECT="project$(echo hacked)id"');
    // Every "$(echo hacked)" occurrence must be backslash-escaped.
    expect(/(^|[^\\])\$\(echo hacked\)/.test(fetchContent)).toBe(false);
  });

  it('produces valid bash scripts with proper escaping', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    // Verify scripts have proper bash shebangs
    const strategicContent = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    expect(strategicContent.startsWith('#!/bin/bash')).toBe(true);

    const fetchContent = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(fetchContent.startsWith('#!/bin/bash')).toBe(true);

    const designDocContent = fs.readFileSync(`${testWorkspace}/design-doc`, 'utf-8');
    expect(designDocContent.startsWith('#!/bin/bash')).toBe(true);
  });

  it('contains critical operational patterns in strategic script', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    const requiredPatterns = [
      'set_problem_model',
      'set_running_update',
      'create_direction',
      'update_direction',
      'retire_direction',
      'set_retirement_critiques',
    ];
    requiredPatterns.forEach(pattern => {
      expect(content).toContain(pattern);
    });
  });

  it('contains critical operational patterns in fetch script', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(content).toContain('directions');
    expect(content).toContain('playbook');
    expect(content).toContain('gaps');
  });

  it('contains critical operational patterns in design-doc script', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/design-doc`, 'utf-8');
    expect(content).toContain('design-doc');
    expect(content).toContain('/api/agent/design-doc');
  });

  it('handles HTTP 409 (ambiguous ref) on message fetch with clear signal', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // Verify the fetch script captures HTTP status and handles 409
    expect(content).toContain('http_code=$(printf');
    expect(content).toContain('if [ "$http_code" -eq 409 ]');
    expect(content).toContain('[AMBIGUOUS]');
    expect(content).toContain('multiple candidates found');
    // 409 should exit with error code (exit 1) so agent knows to retry
    expect(content).toContain('exit 1');
  });

  it('handles HTTP 404 and other non-2xx errors on fetch', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // Verify the fetch script has generic error handling for non-2xx responses
    expect(content).toContain('[ERROR] fetch $KIND failed');
    expect(content).toContain('HTTP $http_code');
    // Non-success responses should exit with error (exit 1)
    expect(content).toMatch(/exit 1[\s\n]*$/);
  });

  it('fetch script returns 0 only on 2xx success', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // First success path should be 2xx check with exit 0
    expect(content).toContain('if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]');
    expect(content).toContain('exit 0');
  });
});

describe('writeFetchHelper (verb-subset read-only fetch)', () => {
  beforeEach(() => {
    if (!fs.existsSync(testWorkspace)) {
      fs.mkdirSync(testWorkspace, { recursive: true });
    }
  });
  afterEach(() => {
    if (fs.existsSync(testWorkspace)) {
      try { fs.rmSync(testWorkspace, { recursive: true }); } catch { /* ignore */ }
    }
  });

  const readFetch = (): string => fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');

  it('full strategic verb set is byte-identical to writeStrategicHelpers output', () => {
    // The strategic path must stay regression-free: the standalone writer with
    // STRATEGIC_FETCH_VERBS must emit the exact same script writeStrategicHelpers
    // emits, so the refactor cannot have changed the strategic agent's tooling.
    writeFetchHelper('t', 'proj-strat', testWorkspace, { verbs: STRATEGIC_FETCH_VERBS });
    const viaWriter = readFetch();
    writeStrategicHelpers('t', 'proj-strat', testWorkspace);
    const viaStrategic = readFetch();
    expect(viaWriter).toBe(viaStrategic);
    // Spot-check the load-bearing usage + case lines the strategic suite pins.
    expect(viaWriter).toContain('fetch problem-model|direction|playbook|gap|message|section|knowledge [id]');
    expect(viaWriter).toContain('problem-model) PATH_SUFFIX="problem-model"');
    expect(viaWriter).toContain('message)    PATH_SUFFIX="messages/$ID"');
    expect(viaWriter).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
  });

  it('bakes ONLY the granted verbs into the case statement (research subset)', () => {
    writeFetchHelper('t', 'proj-1', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const content = readFetch();
    // Granted verbs are real case branches.
    expect(content).toContain('direction)  PATH_SUFFIX="directions/$ID"');
    expect(content).toContain('problem-model) PATH_SUFFIX="problem-model"');
    expect(content).toContain('gap)        PATH_SUFFIX="gaps/$ID"');
    // Non-granted verbs have NO resolving branch.
    expect(content).not.toContain('PATH_SUFFIX="playbook/$ID"');
    expect(content).not.toContain('PATH_SUFFIX="messages/$ID"');
    expect(content).not.toContain('PATH_SUFFIX="design-doc/$ID"');
    // Usage line lists only the granted subset (canonical order; knowledge last).
    expect(content).toContain('Usage: fetch problem-model|direction|gap|knowledge [id]');
  });

  it('rejects a disallowed-but-valid verb with a clear not-permitted message', () => {
    writeFetchHelper('t', 'proj-1', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const content = readFetch();
    // playbook/message/section are full-vocabulary verbs not granted here — they
    // get an explicit reject branch rather than a generic "unknown kind".
    expect(content).toContain('playbook|message|section) echo "Error: ./fetch \'$KIND\' is not permitted in this workspace (allowed: problem-model|direction|gap|knowledge)."');
  });

  it('different subsets bake different verb vocabularies (plan subset)', () => {
    writeFetchHelper('t', 'proj-2', testWorkspace, { verbs: PLAN_FETCH_VERBS });
    const content = readFetch();
    expect(content).toContain('direction)  PATH_SUFFIX="directions/$ID"');
    expect(content).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
    expect(content).toContain('playbook)   PATH_SUFFIX="playbook/$ID"');
    expect(content).toContain('Usage: fetch direction|playbook|section|knowledge [id]');
    // problem-model / gap / message are rejected here.
    expect(content).toContain('problem-model|gap|message) echo "Error: ./fetch \'$KIND\' is not permitted in this workspace (allowed: direction|playbook|section|knowledge)."');
  });

  it('different subsets bake different verb vocabularies (design-doc subset)', () => {
    writeFetchHelper('t', 'proj-3', testWorkspace, { verbs: DESIGN_DOC_FETCH_VERBS });
    const content = readFetch();
    expect(content).toContain('direction)  PATH_SUFFIX="directions/$ID"');
    expect(content).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
    expect(content).toContain('playbook)   PATH_SUFFIX="playbook/$ID"');
    expect(content).toContain('gap)        PATH_SUFFIX="gaps/$ID"');
    expect(content).toContain('message)    PATH_SUFFIX="messages/$ID"');
    expect(content).toContain('Usage: fetch direction|playbook|gap|message|section|knowledge [id]');
    // problem-model is rejected here because it is not part of the design-doc subset.
    expect(content).toContain('problem-model) echo "Error: ./fetch \'$KIND\' is not permitted in this workspace (allowed: direction|playbook|gap|message|section|knowledge)."');
  });

  it('bakes the project id and escapes shell-injection attempts', () => {
    writeFetchHelper('t', 'proj$(echo hacked)id', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const content = readFetch();
    expect(content).toContain('PROJECT="proj\\$(echo hacked)id"');
    expect(content).not.toContain('PROJECT="proj$(echo hacked)id"');
    expect(/(^|[^\\])\$\(echo hacked\)/.test(content)).toBe(false);
  });

  it('writes an executable fetch script and a generalized empty-project guard', () => {
    writeFetchHelper('t', 'proj-1', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const stat = fs.statSync(`${testWorkspace}/fetch`);
    if (process.platform !== 'win32') {
      expect((stat.mode & 0o111) !== 0).toBe(true);
    }
    const content = readFetch();
    expect(content.startsWith('#!/bin/bash')).toBe(true);
    // The empty-project guard is generalized (no longer strategic-only wording).
    expect(content).toContain('no project_id available; ./fetch is unavailable in this workspace');
    expect(content).not.toContain('strategic-mode tasks');
  });

  it('provisions the SEARCH-shaped knowledge verb against the token-scoped endpoint', () => {
    writeFetchHelper('t', 'proj-k', testWorkspace, { verbs: STRATEGIC_FETCH_VERBS });
    const content = readFetch();
    // knowledge is a granted case arm.
    expect(content).toContain('  knowledge)');
    // Get-by-id hits /knowledge-documents/$1 (full body), NOT the project path.
    expect(content).toContain('-X GET "$API/api/agent/knowledge-documents/$1"');
    // List/search hits the bare list endpoint with URL-encoded query filters.
    expect(content).toContain('--get');
    expect(content).toContain('"$API/api/agent/knowledge-documents"');
    expect(content).toContain('--data-urlencode "topic=${2:-}"');
    expect(content).toContain('--data-urlencode "direction=${2:-}"');
    expect(content).toContain('--data-urlencode "q=${2:-}"');
    expect(content).toContain('--data-urlencode "source_kind=${2:-}"');
    expect(content).toContain('--data-urlencode "tag=${2:-}"');
    // It is token-scoped: no project_id is baked into the knowledge URL.
    expect(content).not.toContain('knowledge-documents/$PROJECT');
  });

  it('rejects the knowledge verb in subsets that do not grant it', () => {
    // A bespoke subset without knowledge must reject it as not-permitted.
    writeFetchHelper('t', 'proj-k', testWorkspace, { verbs: ['direction'] });
    const content = readFetch();
    expect(content).not.toContain('  knowledge)\n    shift');
    expect(content).toContain('not permitted in this workspace');
    // The reject branch enumerates knowledge among the disallowed verbs.
    expect(content).toMatch(/knowledge\) echo "Error: \.\/fetch/);
  });
});

describe('buildFetchGuidance', () => {
  it('lists ONLY the granted verbs and advertises ./fetch', () => {
    const block = buildFetchGuidance(RESEARCH_FETCH_VERBS);
    expect(block).toContain('./fetch');
    expect(block).toContain('`./fetch direction <id>`');
    expect(block).toContain('`./fetch problem-model`');
    expect(block).toContain('`./fetch gap <id>`');
    // Verbs not granted to research are absent from the guidance.
    expect(block).not.toContain('`./fetch section');
    expect(block).not.toContain('`./fetch message');
  });

  it('lists the architecture-focused design-doc verbs', () => {
    const block = buildFetchGuidance(DESIGN_DOC_FETCH_VERBS);
    expect(block).toContain('`./fetch direction <id>`');
    expect(block).toContain('`./fetch section <key>`');
    expect(block).toContain('`./fetch playbook <key>`');
    expect(block).toContain('`./fetch gap <id>`');
    expect(block).toContain('`./fetch message <ref>`');
    expect(block).not.toContain('`./fetch problem-model`');
  });

  it('describes the SEARCH-shaped knowledge verb when granted', () => {
    const block = buildFetchGuidance(RESEARCH_FETCH_VERBS);
    expect(block).toContain('`./fetch knowledge');
    expect(block).toContain('--topic');
    expect(block).toContain('--direction');
    // Not present when the subset omits it.
    expect(buildFetchGuidance(['direction'])).not.toContain('`./fetch knowledge');
  });

  it('returns empty string when no verbs are granted', () => {
    expect(buildFetchGuidance([])).toBe('');
  });
});
