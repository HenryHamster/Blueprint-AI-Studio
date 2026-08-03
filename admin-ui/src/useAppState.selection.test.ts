import { describe, expect, test } from 'vitest';
import { effectiveSelectedProjectId } from './useAppState';

describe('effectiveSelectedProjectId', () => {
  test('keeps persisted selection inert until membership is authoritative', () => {
    expect(effectiveSelectedProjectId('project-1', false, null, true)).toBeNull();
    expect(effectiveSelectedProjectId('project-1', true, 'Failed to load projects', true)).toBeNull();
    expect(effectiveSelectedProjectId('project-1', true, null, false)).toBeNull();
  });

  test('releases only a loaded access-qualified selection', () => {
    expect(effectiveSelectedProjectId('project-1', true, null, true)).toBe('project-1');
    expect(effectiveSelectedProjectId(null, true, null, true)).toBeNull();
  });
});
