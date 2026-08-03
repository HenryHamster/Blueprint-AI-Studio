import type { Project } from './types/project';

export type PlatformRole = 'admin' | 'user' | null | undefined;
export type ProjectRole = Project['my_role'] | null | undefined;

/**
 * Frontend projection of Blueprint's existing platform and project roles.
 *
 * This is deliberately a small adapter, not a second permission store. The
 * backend remains authoritative. Callers use these flags to avoid presenting
 * controls or issuing requests that the backend will reject. Unknown and
 * still-loading roles fail closed.
 */
export interface AccessProfile {
  isPlatformAdmin: boolean;
  projectRole: Exclude<ProjectRole, null | undefined> | null;
  canViewProject: boolean;
  canOperateProject: boolean;
  canManageProject: boolean;
  canReadPlatformSettings: boolean;
  canManagePlatform: boolean;
}

const PROJECT_ROLES = new Set(['owner', 'member', 'viewer'] as const);

export function resolveAccess(
  platformRole: PlatformRole,
  projectRole: ProjectRole,
): AccessProfile {
  const normalizedProjectRole = projectRole && PROJECT_ROLES.has(projectRole)
    ? projectRole
    : null;
  const isPlatformAdmin = platformRole === 'admin';
  const canOperateProject = normalizedProjectRole === 'owner' || normalizedProjectRole === 'member';

  return {
    isPlatformAdmin,
    projectRole: normalizedProjectRole,
    canViewProject: normalizedProjectRole !== null,
    canOperateProject,
    canManageProject: normalizedProjectRole === 'owner',
    canReadPlatformSettings: isPlatformAdmin,
    canManagePlatform: isPlatformAdmin,
  };
}
