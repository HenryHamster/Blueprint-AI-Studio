import { describe, expect, it } from 'vitest';
import {
  isWorkbenchEventEnvelopeV1,
  workbenchRefetchSurfacesFromData,
  workbenchRefetchSurfacesFromMessage,
  workbenchMessageMatchesProject,
} from './workbenchEvents';

describe('workbench event envelope helpers', () => {
  it('recognizes v1 workbench envelopes without requiring UI to render them', () => {
    expect(isWorkbenchEventEnvelopeV1({
      schema_version: 1,
      project_id: 'project-1',
      resource: 'local_runner',
      action: 'heartbeat',
      refetch: { surfaces: ['strategic_session', 'runtime'] },
    })).toBe(true);

    expect(isWorkbenchEventEnvelopeV1({
      schema_version: 2,
      project_id: 'project-1',
      resource: 'local_runner',
      action: 'heartbeat',
    })).toBe(false);

    expect(isWorkbenchEventEnvelopeV1({
      schema_version: 1,
      project_id: 'project-1',
      resource: 'local_runner',
    })).toBe(false);
  });

  it('extracts refetch surfaces from both envelope and legacy payloads', () => {
    expect(Array.from(workbenchRefetchSurfacesFromData({
      schema_version: 1,
      project_id: 'project-1',
      resource: 'project_message',
      action: 'created',
      refetch: { surfaces: ['messages', 'strategic_session', 'messages', 12] },
    }))).toEqual(['messages', 'strategic_session']);

    expect(Array.from(workbenchRefetchSurfacesFromMessage({
      event: 'workbench_refetch',
      data: { refetch: { surfaces: ['directions'] } },
    }))).toEqual(['directions']);

    expect(Array.from(workbenchRefetchSurfacesFromMessage({ event: 'noop' }))).toEqual([]);
  });

  it('filters v1 envelope refetch surfaces by the active project', () => {
    const message = {
      event: 'workbench_refetch',
      data: {
        schema_version: 1,
        project_id: 'project-1',
        resource: 'local_runner',
        action: 'registered',
        refetch: { surfaces: ['runtime', 'strategic_session'] },
      },
    };

    expect(Array.from(workbenchRefetchSurfacesFromMessage(message, 'project-1'))).toEqual([
      'runtime',
      'strategic_session',
    ]);
    expect(Array.from(workbenchRefetchSurfacesFromMessage(message, 'project-2'))).toEqual([]);
    expect(workbenchMessageMatchesProject(message, 'project-1')).toBe(true);
    expect(workbenchMessageMatchesProject(message, 'project-2')).toBe(false);
  });

  it('does not project-filter legacy refetch payloads without an envelope', () => {
    expect(Array.from(workbenchRefetchSurfacesFromMessage({
      event: 'workbench_refetch',
      data: { refetch: { surfaces: ['messages'] } },
    }, 'project-2'))).toEqual(['messages']);
  });
});
