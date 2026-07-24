import { describe, expect, it } from 'vitest';
import {
  normalizeWorkbenchRefetchSurfaces,
  normalizeWorkbenchTarget,
} from './workbenchTargets';

describe('workbench target helpers', () => {
  it('normalizes canonical target fields and drops unknown keys', () => {
    expect(normalizeWorkbenchTarget({
      surface: ' runtime ',
      section: ' devices ',
      runner_id: ' runner-1 ',
      secret: 'drop-me',
    } as unknown as Parameters<typeof normalizeWorkbenchTarget>[0])).toEqual({
      surface: 'runtime',
      section: 'devices',
      runner_id: 'runner-1',
    });
  });

  it('rejects unknown surfaces and drops unknown constrained sections', () => {
    expect(normalizeWorkbenchTarget({
      surface: 'debug',
      section: 'raw',
    })).toBeNull();

    expect(normalizeWorkbenchTarget({
      surface: 'runtime',
      section: 'runners',
      runner_id: 'runner-1',
    })).toEqual({
      surface: 'runtime',
      section: 'devices',
      runner_id: 'runner-1',
    });

    expect(normalizeWorkbenchTarget({
      surface: 'runtime',
      section: 'debug-panel',
      runner_id: 'runner-1',
    })).toEqual({
      surface: 'runtime',
      runner_id: 'runner-1',
    });
  });

  it('dedupes refetch surfaces and filters unsupported values', () => {
    expect(normalizeWorkbenchRefetchSurfaces([
      'runtime',
      'debug',
      'strategic_session',
      'runtime',
      12,
    ])).toEqual(['runtime', 'strategic_session']);
  });
});
