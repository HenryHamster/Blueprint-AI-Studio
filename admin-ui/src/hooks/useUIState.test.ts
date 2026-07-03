import { describe, expect, test } from 'vitest';
import { normalizePrimaryTab, resolvePrimaryTabTarget } from './useUIState';

describe('primary tab normalization', () => {
  test('keeps canonical workbench tabs unchanged', () => {
    expect(normalizePrimaryTab('chat')).toBe('chat');
    expect(normalizePrimaryTab('pipeline')).toBe('pipeline');
    expect(normalizePrimaryTab('project')).toBe('project');
    expect(normalizePrimaryTab('runtime')).toBe('runtime');
  });

  test('maps legacy tab aliases to canonical workbench targets', () => {
    expect(resolvePrimaryTabTarget('cockpit')).toEqual({
      tab: 'project',
      projectSection: 'running-update',
    });
    expect(resolvePrimaryTabTarget('directions')).toEqual({
      tab: 'project',
      projectSection: 'directions',
    });
    expect(resolvePrimaryTabTarget('home')).toEqual({
      tab: 'runtime',
      runtimeSection: 'health',
    });
    expect(resolvePrimaryTabTarget('compute')).toEqual({
      tab: 'runtime',
      runtimeSection: 'compute',
    });
    expect(resolvePrimaryTabTarget('board')).toEqual({
      tab: 'pipeline',
      pipelineSection: 'board',
      viewMode: 'kanban',
      showArchive: false,
    });
  });

  test('falls back unknown values to Strategic chat', () => {
    expect(resolvePrimaryTabTarget('missing')).toEqual({ tab: 'chat' });
    expect(resolvePrimaryTabTarget(null)).toEqual({ tab: 'chat' });
  });
});
