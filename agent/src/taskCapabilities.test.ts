import { describe, expect, it } from 'vitest';
import {
  ALL_AGENT_TASK_TYPES,
  READ_ONLY_TASK_TYPES,
  TASK_CAPABILITIES,
  WRITE_TASK_TYPES,
  isReadOnlyTaskType,
} from './taskCapabilities.js';

describe('task capability registry', () => {
  it('has no duplicate task ids', () => {
    expect(new Set(ALL_AGENT_TASK_TYPES).size).toBe(ALL_AGENT_TASK_TYPES.length);
  });

  it('partitions task ids into read-only and write modes', () => {
    const readOnly = new Set<string>(READ_ONLY_TASK_TYPES);
    const write = new Set<string>(WRITE_TASK_TYPES);
    for (const taskType of ALL_AGENT_TASK_TYPES) {
      expect(readOnly.has(taskType) || write.has(taskType)).toBe(true);
      expect(readOnly.has(taskType) && write.has(taskType)).toBe(false);
    }
  });

  it('keeps registry entries metadata-only', () => {
    expect(TASK_CAPABILITIES.every(capability => 'id' in capability && 'agentMode' in capability)).toBe(true);
    expect(TASK_CAPABILITIES.some(capability => 'payloadFields' in capability)).toBe(false);
  });

  it('exposes read-only checks for permission enforcement', () => {
    expect(isReadOnlyTaskType('self_audit')).toBe(true);
    expect(isReadOnlyTaskType('create')).toBe(false);
    expect(isReadOnlyTaskType('future_capability')).toBe(false);
  });
});
