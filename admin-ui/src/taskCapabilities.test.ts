import { describe, expect, it } from 'vitest';
import {
  FILTER_TASK_TYPE_IDS,
  PROVIDER_ROUTABLE_TASK_TYPE_IDS,
  TASK_CAPABILITIES,
  USER_LAUNCHABLE_TASK_TYPE_IDS,
  getTaskTypeLabel,
} from './taskCapabilities';

describe('task capability registry', () => {
  it('has no duplicate task ids', () => {
    const ids = TASK_CAPABILITIES.map(capability => capability.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('keeps user-launchable and provider-routable subsets inside known capabilities', () => {
    const ids = new Set(TASK_CAPABILITIES.map(capability => capability.id));
    expect(USER_LAUNCHABLE_TASK_TYPE_IDS.every(id => ids.has(id))).toBe(true);
    expect(PROVIDER_ROUTABLE_TASK_TYPE_IDS.every(id => ids.has(id))).toBe(true);
    expect(FILTER_TASK_TYPE_IDS.every(id => ids.has(id))).toBe(true);
  });

  it('keeps rollup as a UI-only synthetic type', () => {
    expect(FILTER_TASK_TYPE_IDS).toContain('rollup');
    expect(PROVIDER_ROUTABLE_TASK_TYPE_IDS).not.toContain('rollup');
    expect(USER_LAUNCHABLE_TASK_TYPE_IDS).not.toContain('rollup');
  });

  it('includes backend task capabilities that were previously hidden from filters', () => {
    expect(FILTER_TASK_TYPE_IDS).toContain('self_audit');
    expect(PROVIDER_ROUTABLE_TASK_TYPE_IDS).toContain('investigate');
  });

  it('does not invent labels for unknown future task types', () => {
    expect(getTaskTypeLabel('future_capability')).toBe('future_capability');
  });
});
