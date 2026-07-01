import { describe, expect, test } from 'vitest';
import type { StrategicStateAttentionItem } from '../strategicStateApi';
import { pipelineTaskIdForAttentionItem } from './strategicAttention';

function makeAttentionItem(overrides: Partial<StrategicStateAttentionItem> = {}): StrategicStateAttentionItem {
  return {
    id: 'task:task-1:stale_provisioning_task',
    kind: 'task',
    severity: 'needs_operator',
    reason: 'stale_provisioning_task',
    label: 'Provisioning stale',
    ref: { kind: 'task', id: 'task-1', project_id: 'proj-1' },
    ...overrides,
  };
}

describe('pipelineTaskIdForAttentionItem', () => {
  test('returns the explicit task target for task attention', () => {
    expect(pipelineTaskIdForAttentionItem(makeAttentionItem({
      target: { surface: 'task', task_id: 'task-target' },
    }))).toBe('task-target');
  });

  test('falls back to task_id and task ref for task attention', () => {
    expect(pipelineTaskIdForAttentionItem(makeAttentionItem({ task_id: 'task-field' }))).toBe('task-field');
    expect(pipelineTaskIdForAttentionItem(makeAttentionItem())).toBe('task-1');
  });

  test('does not promote task-scoped alerts into pipeline task attention', () => {
    expect(pipelineTaskIdForAttentionItem(makeAttentionItem({
      id: 'alert:task-1:runner',
      kind: 'alert',
      reason: 'runner_failed',
      label: 'Runner alert',
      target: { surface: 'task', task_id: 'task-1' },
      task_id: 'task-1',
    }))).toBeNull();
  });
});
