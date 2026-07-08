import { describe, expect, test } from 'vitest';
import { formatBackendInventory } from './FleetRunnersPanel.utils';
import type { FleetRunner } from '../api';

type Caps = FleetRunner['capabilities'];

describe('formatBackendInventory', () => {
  test('maps every truthy isolation-backend flag to its short label in order', () => {
    const caps: Caps = {
      isolation_backends: {
        bwrap_ok: true,
        docker_usable: true,
        podman_usable: true,
        apptainer_ok: true,
        enroot_ok: true,
      },
    };
    expect(formatBackendInventory(caps).available).toEqual([
      'bwrap',
      'docker',
      'podman',
      'apptainer',
      'enroot',
    ]);
  });

  test('includes only the backends whose flags are true', () => {
    const caps: Caps = {
      isolation_backends: { bwrap_ok: true, docker_usable: false, podman_usable: true },
    };
    expect(formatBackendInventory(caps).available).toEqual(['bwrap', 'podman']);
  });

  test('returns empty available list when isolation_backends is absent (older runner)', () => {
    const result = formatBackendInventory({ task_types: 'all' });
    expect(result.available).toEqual([]);
    expect(result.agentBackend).toBeNull();
    expect(result.computeBackend).toBeNull();
  });

  test('surfaces selected agent and compute backends when present', () => {
    const caps: Caps = {
      isolation_backends: { bwrap_ok: true, docker_usable: true },
      agent_backend: 'bwrap',
      compute_backend: 'docker',
    };
    const result = formatBackendInventory(caps);
    expect(result.agentBackend).toBe('bwrap');
    expect(result.computeBackend).toBe('docker');
  });

  test('agent/compute backends default to null when the runner omits them', () => {
    const caps: Caps = { isolation_backends: { bwrap_ok: true } };
    const result = formatBackendInventory(caps);
    expect(result.agentBackend).toBeNull();
    expect(result.computeBackend).toBeNull();
  });
});
