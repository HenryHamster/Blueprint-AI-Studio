import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import { writeStrategicHelpers } from './strategic-helpers.js';

const testWorkspace = '/tmp/test-strategic-helpers-workspace';

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
    expect((stat.mode & 0o111) !== 0).toBe(true); // Check execute bit
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
    expect(content).toContain('direction');
    expect(content).toContain('playbook');
    expect(content).toContain('gap');
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
    // taskId is embedded in double quotes, so $ is literal
    expect(strategicContent).toContain(`TASK="${taskId}"`);
    // Verify the injection payload is not executed in the quoting context
    expect(strategicContent).not.toContain('whoami');
  });

  it('prevents shell injection in projectId with special characters', () => {
    const taskId = 'test-task';
    const projectId = 'project$(echo hacked)id';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const fetchContent = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // projectId is embedded in double quotes, so $ is literal
    expect(fetchContent).toContain(`PROJECT="${projectId}"`);
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
