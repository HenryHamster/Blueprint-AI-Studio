import { describe, expect, test } from 'vitest';
import { resolveAccess, type PlatformRole, type ProjectRole } from './accessControl';

describe('resolveAccess role personas', () => {
  test.each<{
    persona: string;
    platformRole: PlatformRole;
    projectRole: ProjectRole;
    platformSettings: boolean;
    view: boolean;
    operate: boolean;
    manage: boolean;
  }>([
    {
      persona: 'platform admin and project owner',
      platformRole: 'admin',
      projectRole: 'owner',
      platformSettings: true,
      view: true,
      operate: true,
      manage: true,
    },
    {
      persona: 'ordinary project owner',
      platformRole: 'user',
      projectRole: 'owner',
      platformSettings: false,
      view: true,
      operate: true,
      manage: true,
    },
    {
      persona: 'project member',
      platformRole: 'user',
      projectRole: 'member',
      platformSettings: false,
      view: true,
      operate: true,
      manage: false,
    },
    {
      persona: 'project viewer',
      platformRole: 'user',
      projectRole: 'viewer',
      platformSettings: false,
      view: true,
      operate: false,
      manage: false,
    },
  ])('$persona has the expected request eligibility', ({
    platformRole,
    projectRole,
    platformSettings,
    view,
    operate,
    manage,
  }) => {
    const access = resolveAccess(platformRole, projectRole);

    expect(access.canReadPlatformSettings).toBe(platformSettings);
    expect(access.canViewProject).toBe(view);
    expect(access.canOperateProject).toBe(operate);
    expect(access.canManageProject).toBe(manage);
  });

  test.each([
    { platformRole: undefined, projectRole: undefined },
    { platformRole: null, projectRole: null },
    { platformRole: 'user' as const, projectRole: undefined },
  ])('unknown or loading roles fail closed: %o', ({ platformRole, projectRole }) => {
    expect(resolveAccess(platformRole, projectRole)).toEqual({
      isPlatformAdmin: false,
      projectRole: null,
      canViewProject: false,
      canOperateProject: false,
      canManageProject: false,
      canReadPlatformSettings: false,
      canManagePlatform: false,
    });
  });

  test('platform admin authority does not invent project membership', () => {
    const access = resolveAccess('admin', undefined);

    expect(access.isPlatformAdmin).toBe(true);
    expect(access.canReadPlatformSettings).toBe(true);
    expect(access.canViewProject).toBe(false);
    expect(access.canOperateProject).toBe(false);
    expect(access.canManageProject).toBe(false);
  });
});
