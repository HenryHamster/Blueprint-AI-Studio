import { createContext, createElement, useContext, useMemo } from 'react';
import type { ReactNode } from 'react';
import type { TaskCapabilityContract, TaskCapabilityContractEntry } from './types/settings';

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
    id: 'design',
    label: 'Design',
    description: 'Codebase-internal design document',
    userLaunchable: true,
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
  {
    id: 'compute_experiment',
    label: 'Compute Experiment',
    description: 'Run a budgeted compute job',
    userLaunchable: false,
    providerConfigurable: false,
    filterVisible: true,
    synthetic: false,
  },
] as const;

type TaskCapability = (typeof TASK_CAPABILITIES)[number];
export type TaskType = TaskCapability['id'];
export type UserLaunchableTaskType = Extract<TaskCapability, { userLaunchable: true }>['id'];

export const TASK_CAPABILITY_BY_ID = Object.fromEntries(
  TASK_CAPABILITIES.map(capability => [capability.id, capability]),
) as Record<TaskType, TaskCapability>;

export const FILTER_TASK_TYPE_IDS: readonly TaskType[] = TASK_CAPABILITIES
  .filter(capability => capability.filterVisible)
  .map(capability => capability.id);

export const USER_LAUNCHABLE_TASK_TYPE_IDS: readonly UserLaunchableTaskType[] = TASK_CAPABILITIES
  .filter((capability): capability is Extract<TaskCapability, { userLaunchable: true }> => capability.userLaunchable)
  .map(capability => capability.id);

/**
 * Shape of a launchable-task-type option rendered by the New Task launcher.
 * `value` is a plain string (not the compile-time `UserLaunchableTaskType`
 * union) because the launcher now derives its options from the backend
 * catalog, which can include types registered after this static list was
 * authored.
 */
export interface LaunchableTaskTypeOption {
  value: string;
  label: string;
  description: string;
}

export const USER_LAUNCHABLE_TASK_TYPE_OPTIONS: readonly LaunchableTaskTypeOption[] =
  USER_LAUNCHABLE_TASK_TYPE_IDS.map(id => ({
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

// ─── Backend-published-contract resolution ─────────────────────────────────
//
// The operator UI consumes the backend's published task-capability contract
// (the `task_capabilities` map in GET /settings) so newly registered task
// types surface automatically without editing this file. The static table
// above is the *compatibility fallback*: it seeds the resolved capabilities
// when talking to an older backend that does not yet publish the contract, and
// it supplies the UI-only synthetic entries (e.g. `rollup`) the backend never
// publishes. There is intentionally NO second backend registry here — the
// fallback is the same table that already drives the legacy exports.

// A capability normalized into the shape every operator surface consumes,
// regardless of whether it came from the published contract or the fallback.
export interface ResolvedCapability {
  id: string;
  label: string;
  badge: string;
  description: string;
  userLaunchable: boolean;
  // True for a non-user-launchable type that still has a dedicated structured
  // launch-contract path (compute_experiment). Such types are surfaced in the
  // new-task launcher alongside user-launchable ones so the operator can reach
  // the dedicated contract form.
  operatorContractLaunchable: boolean;
  providerConfigurable: boolean;
  filterVisible: boolean;
  reservable: boolean;
  synthetic: boolean;
}

export interface ResolvedTaskCapabilities {
  all: ResolvedCapability[];
  byId: Record<string, ResolvedCapability>;
  filterIds: string[];
  userLaunchableOptions: { value: string; label: string; description: string }[];
  providerRoutableIds: string[];
  providerRoutableOptions: { value: string; label: string }[];
  filterOptions: { value: string; label: string }[];
  reservableIds: string[];
  getCapability: (id: string) => ResolvedCapability | undefined;
  getLabel: (id: string) => string;
}

// Reservable task types in the fallback (older-backend) path. Mirrors the
// backend RESERVABLE_TASK_TYPES set; only consulted when the published
// contract is absent, since the contract carries `reservable` per entry.
const FALLBACK_RESERVABLE_IDS = new Set<string>(['create', 'plan', 'research']);

const STATIC_RESOLVED: ResolvedCapability[] = TASK_CAPABILITIES.map(capability => ({
  id: capability.id,
  label: capability.label,
  badge: capability.label,
  description: capability.description,
  userLaunchable: capability.userLaunchable,
  // The static fallback table has no contract-launchable types; the dedicated
  // compute_experiment path only surfaces when the backend publishes the flag.
  operatorContractLaunchable: false,
  providerConfigurable: capability.providerConfigurable,
  filterVisible: capability.filterVisible,
  reservable: FALLBACK_RESERVABLE_IDS.has(capability.id),
  synthetic: capability.synthetic,
}));

function normalizePublished(entry: TaskCapabilityContractEntry): ResolvedCapability {
  const label = entry.label ?? entry.type;
  return {
    id: entry.type,
    label,
    badge: entry.badge ?? label,
    description: entry.description ?? '',
    userLaunchable: entry.user_launchable ?? false,
    operatorContractLaunchable: entry.operator_contract_launchable ?? false,
    providerConfigurable: entry.provider_configurable ?? false,
    filterVisible: entry.filter_visible ?? false,
    reservable: entry.reservable ?? false,
    // The backend registry has no notion of synthetic UI-only types; anything
    // it publishes is a real, backend-backed task type.
    synthetic: false,
  };
}

function buildResolved(caps: ResolvedCapability[]): ResolvedTaskCapabilities {
  const byId: Record<string, ResolvedCapability> = {};
  for (const cap of caps) byId[cap.id] = cap;
  const getCapability = (id: string) => byId[id];
  const filterCaps = caps.filter(c => c.filterVisible);
  return {
    all: caps,
    byId,
    filterIds: filterCaps.map(c => c.id),
    userLaunchableOptions: caps
      .filter(c => (c.userLaunchable || c.operatorContractLaunchable) && !c.synthetic)
      .map(c => ({ value: c.id, label: c.label, description: c.description })),
    providerRoutableIds: caps
      .filter(c => c.providerConfigurable && !c.synthetic)
      .map(c => c.id),
    providerRoutableOptions: caps
      .filter(c => c.providerConfigurable && !c.synthetic)
      .map(c => ({ value: c.id, label: c.label })),
    filterOptions: filterCaps.map(c => ({ value: c.id, label: c.label })),
    reservableIds: caps.filter(c => c.reservable && !c.synthetic).map(c => c.id),
    getCapability,
    getLabel: (id: string) => getCapability(id)?.label ?? id,
  };
}

export function matchesTaskTypeFilter(
  taskType: string,
  selectedTaskTypes: readonly string[],
  knownTaskTypes: readonly string[],
): boolean {
  if (selectedTaskTypes.includes(taskType)) return true;
  if (selectedTaskTypes.length !== knownTaskTypes.length) return false;
  return !knownTaskTypes.includes(taskType);
}

// Resolve the capability set the UI should render. When the backend publishes a
// non-empty contract we use it as the source of truth and overlay only the
// UI-only synthetic entries (rollup) it never publishes; otherwise we fall back
// to the built-in static table (fail closed to known behavior).
export function resolveTaskCapabilities(
  published?: TaskCapabilityContract | null,
): ResolvedTaskCapabilities {
  if (published && Object.keys(published).length > 0) {
    const publishedCaps = Object.values(published).map(normalizePublished);
    const publishedIds = new Set(publishedCaps.map(c => c.id));
    const syntheticUi = STATIC_RESOLVED.filter(c => c.synthetic && !publishedIds.has(c.id));
    return buildResolved([...publishedCaps, ...syntheticUi]);
  }
  return buildResolved(STATIC_RESOLVED);
}

// Resolved fallback computed once; also the default context value so consumers
// rendered without a provider (and unit tests) keep working unchanged.
export const FALLBACK_RESOLVED_TASK_CAPABILITIES = resolveTaskCapabilities();

const TaskCapabilitiesContext = createContext<ResolvedTaskCapabilities>(
  FALLBACK_RESOLVED_TASK_CAPABILITIES,
);

export function TaskCapabilitiesProvider({
  contract,
  children,
}: {
  contract: TaskCapabilityContract | null | undefined;
  children: ReactNode;
}) {
  const value = useMemo(() => resolveTaskCapabilities(contract), [contract]);
  return createElement(TaskCapabilitiesContext.Provider, { value }, children);
}

// Hook every operator surface uses to read the resolved, contract-driven
// capability set. Returns the fallback when no provider is mounted.
export function useTaskCapabilities(): ResolvedTaskCapabilities {
  return useContext(TaskCapabilitiesContext);
}
