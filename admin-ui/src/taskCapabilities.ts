export const TASK_CAPABILITIES = [
  {
    id: 'create',
    label: 'Create',
    description: 'Write code, open PR',
    userLaunchable: true,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'review',
    label: 'Review',
    description: 'Review existing PR',
    userLaunchable: true,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'fix',
    label: 'Fix',
    description: 'Fix PR feedback',
    userLaunchable: true,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'plan',
    label: 'Plan',
    description: 'Decompose into subtasks',
    userLaunchable: true,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'research',
    label: 'Research',
    description: 'Analyze codebase',
    userLaunchable: true,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'strategic_review',
    label: 'Strategic Review',
    description: 'Review project direction',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'design_doc_update',
    label: 'Design Doc',
    description: 'Update architecture docs',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'security_audit',
    label: 'Security Audit',
    description: 'Audit security risks',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'qa',
    label: 'QA',
    description: 'Add or run tests',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'deps',
    label: 'Deps',
    description: 'Update dependencies',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'docs',
    label: 'Docs',
    description: 'Update docs',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'migrate',
    label: 'Migrate',
    description: 'Generate DB migration',
    userLaunchable: true,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'smoketest',
    label: 'Smoketest',
    description: 'Run smoke checks',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'refactor',
    label: 'Refactor',
    description: 'Improve code quality',
    userLaunchable: true,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'debug',
    label: 'Debug',
    description: 'Diagnose a bug',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'perf',
    label: 'Perf',
    description: 'Profile bottlenecks',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'investigate',
    label: 'Investigate',
    description: 'Read-only investigation',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'self_audit',
    label: 'Self Audit',
    description: 'Score system maturity',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'memo',
    label: 'Memo',
    description: 'Reasoning artifact',
    userLaunchable: false,
    providerConfigurable: false,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'verify_tests',
    label: 'Verify Tests',
    description: 'Run repo tests as the no-CI merge gate',
    userLaunchable: false,
    providerConfigurable: true,
    filterVisible: true,
    synthetic: false,
  },
  {
    id: 'rollup',
    label: 'Rollup',
    description: 'Rollup PR aggregation',
    userLaunchable: false,
    providerConfigurable: false,
    filterVisible: true,
    synthetic: true,
  },
] as const;

export type TaskCapability = (typeof TASK_CAPABILITIES)[number];
export type TaskType = TaskCapability['id'];
export type BackendTaskType = Extract<TaskCapability, { synthetic: false }>['id'];
export type UserLaunchableTaskType = Extract<TaskCapability, { userLaunchable: true }>['id'];

export const TASK_CAPABILITY_BY_ID = Object.fromEntries(
  TASK_CAPABILITIES.map(capability => [capability.id, capability]),
) as Record<TaskType, TaskCapability>;

export const FILTER_TASK_TYPE_IDS: readonly TaskType[] = TASK_CAPABILITIES
  .filter(capability => capability.filterVisible)
  .map(capability => capability.id);

export const PROVIDER_ROUTABLE_TASK_TYPE_IDS: readonly BackendTaskType[] = TASK_CAPABILITIES
  .filter((capability): capability is Extract<TaskCapability, { synthetic: false; providerConfigurable: true }> => (
    !capability.synthetic && capability.providerConfigurable
  ))
  .map(capability => capability.id);

export const USER_LAUNCHABLE_TASK_TYPE_IDS: readonly UserLaunchableTaskType[] = TASK_CAPABILITIES
  .filter((capability): capability is Extract<TaskCapability, { userLaunchable: true }> => capability.userLaunchable)
  .map(capability => capability.id);

export const FILTER_TASK_TYPE_OPTIONS = FILTER_TASK_TYPE_IDS.map(id => ({
  value: id,
  label: TASK_CAPABILITY_BY_ID[id].label,
}));

export const PROVIDER_ROUTABLE_TASK_TYPE_OPTIONS = PROVIDER_ROUTABLE_TASK_TYPE_IDS.map(id => ({
  value: id,
  label: TASK_CAPABILITY_BY_ID[id].label,
}));

export const USER_LAUNCHABLE_TASK_TYPE_OPTIONS = USER_LAUNCHABLE_TASK_TYPE_IDS.map(id => ({
  value: id,
  label: TASK_CAPABILITY_BY_ID[id].label,
  description: TASK_CAPABILITY_BY_ID[id].description,
}));

export function getTaskCapability(taskType: string): TaskCapability | undefined {
  return TASK_CAPABILITY_BY_ID[taskType as TaskType];
}

export function getTaskTypeLabel(taskType: string): string {
  return getTaskCapability(taskType)?.label ?? taskType;
}
