import { describe, expect, test, vi } from 'vitest';
import { refreshAndSelectProject } from './projectSelection';

describe('refreshAndSelectProject', () => {
  test('selects the created project only after the refreshed list contains it', async () => {
    const calls: string[] = [];
    const refreshProjects = vi.fn(async () => {
      calls.push('refresh');
      return [{ id: 'created-project' }];
    });
    const selectProject = vi.fn(() => calls.push('select'));

    await expect(refreshAndSelectProject('created-project', refreshProjects, selectProject)).resolves.toBe(true);

    expect(selectProject).toHaveBeenCalledWith('created-project');
    expect(calls).toEqual(['refresh', 'select']);
  });

  test('does not persist a selection when refresh fails or omits the project', async () => {
    const selectProject = vi.fn();

    await expect(refreshAndSelectProject('created-project', async () => null, selectProject)).resolves.toBe(false);
    await expect(refreshAndSelectProject('created-project', async () => [{ id: 'other' }], selectProject)).resolves.toBe(false);

    expect(selectProject).not.toHaveBeenCalled();
  });
});
