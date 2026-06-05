import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { writeStrategicHelpers } from './strategic-helpers.js';

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
    expect(content).toContain('fetch problem-model|direction|playbook|gap|message [id]');
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
    expect(content).toContain('Usage: fetch problem-model|direction|playbook|gap|message [id]');
    expect(content).toContain('problem-model) PATH_SUFFIX="problem-model"');
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
});
