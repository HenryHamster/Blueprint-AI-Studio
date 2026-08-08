export const WORKBENCH_TARGET_SURFACES = [
  'messages',
  'pipeline',
  'project',
  'runtime',
  'task',
  'knowledge',
] as const;

export const WORKBENCH_REFETCH_SURFACES = [
  'messages',
  'strategic_session',
  'directions',
  'pipeline',
  'runtime',
] as const;

export const WORKBENCH_PROJECT_SECTIONS = [
  'running-update',
  'directions',
  'problem-model',
  'design-doc',
  'manage',
  'selected_refs',
  'overview',
  'build-config',
  'branches',
  'auto-review',
  'resource-envelope',
  'auto-remediation',
  'issue-implementation',
  'alert-domains',
  'access',
] as const;

export const WORKBENCH_PIPELINE_SECTIONS = ['flow', 'board', 'archive'] as const;

export const WORKBENCH_RUNTIME_SECTIONS = [
  'health',
  'devices',
  'compute',
  'accounts',
  'settings',
  'analytics',
  'general',
  'orchestrator',
  'deploy',
  'provider-routing',
  'provider-settings',
  'notifications',
  'advanced',
  // RuntimeWorkspace renders VersionLifecyclePanel here; attention items that
  // deep-link to promotion/activation target it (mirrors RUNTIME_SECTIONS in
  // backend/app/workbench_contracts.py).
  'version-lifecycle',
] as const;

const WORKBENCH_SURFACE_ALIASES = {
  chat: 'messages',
  message: 'messages',
} as const;

const WORKBENCH_RUNTIME_SECTION_ALIASES = {
  runner: 'devices',
  runners: 'devices',
  local_runner: 'devices',
  'local-runners': 'devices',
  fleet: 'devices',
} as const;

const WORKBENCH_TARGET_KEYS = [
  'project_id',
  'section',
  'task_id',
  'message_id',
  'direction_id',
  'alert_id',
  'action',
  'document_id',
  'artifact_id',
  'cycle_id',
  'runner_id',
] as const;

export type WorkbenchTargetSurface = typeof WORKBENCH_TARGET_SURFACES[number];
export type WorkbenchRefetchSurface = typeof WORKBENCH_REFETCH_SURFACES[number];
export type WorkbenchProjectSection = typeof WORKBENCH_PROJECT_SECTIONS[number];
export type WorkbenchPipelineSection = typeof WORKBENCH_PIPELINE_SECTIONS[number];
export type WorkbenchRuntimeSection = typeof WORKBENCH_RUNTIME_SECTIONS[number];

export type WorkbenchTargetInput = {
  surface?: string | null;
  project_id?: string | null;
  section?: string | null;
  task_id?: string | null;
  message_id?: string | null;
  direction_id?: string | null;
  alert_id?: string | null;
  action?: string | null;
  document_id?: string | null;
  artifact_id?: string | null;
  cycle_id?: string | null;
  runner_id?: string | null;
};

export type NormalizedWorkbenchTarget = {
  surface: WorkbenchTargetSurface;
} & Partial<Record<typeof WORKBENCH_TARGET_KEYS[number], string>>;

function compactText(value: unknown): string | null {
  if (value == null) return null;
  const text = String(value).trim().replace(/\s+/g, ' ');
  return text || null;
}

function isOneOf<const T extends readonly string[]>(values: T, value: string | null): value is T[number] {
  return Boolean(value && (values as readonly string[]).includes(value));
}

export function isWorkbenchTargetSurface(value: unknown): value is WorkbenchTargetSurface {
  return isOneOf(WORKBENCH_TARGET_SURFACES, compactText(value));
}

export function isWorkbenchProjectSection(value?: string | null): value is WorkbenchProjectSection {
  return isOneOf(WORKBENCH_PROJECT_SECTIONS, compactText(value));
}

export function isWorkbenchPipelineSection(value?: string | null): value is WorkbenchPipelineSection {
  return isOneOf(WORKBENCH_PIPELINE_SECTIONS, compactText(value));
}

export function isWorkbenchRuntimeSection(value?: string | null): value is WorkbenchRuntimeSection {
  return isOneOf(WORKBENCH_RUNTIME_SECTIONS, compactText(value));
}

export function normalizeWorkbenchRuntimeSection(value?: string | null): WorkbenchRuntimeSection | null {
  const section = compactText(value);
  if (!section) return null;
  const aliased = WORKBENCH_RUNTIME_SECTION_ALIASES[section as keyof typeof WORKBENCH_RUNTIME_SECTION_ALIASES] ?? section;
  return isWorkbenchRuntimeSection(aliased) ? aliased : null;
}

function normalizeWorkbenchSurface(value: unknown): WorkbenchTargetSurface | null {
  const surface = compactText(value);
  if (!surface) return null;
  const aliased = WORKBENCH_SURFACE_ALIASES[surface as keyof typeof WORKBENCH_SURFACE_ALIASES] ?? surface;
  return isWorkbenchTargetSurface(aliased) ? aliased : null;
}

function normalizeWorkbenchSection(surface: WorkbenchTargetSurface, value: unknown): string | null {
  const section = compactText(value);
  if (!section) return null;
  if (surface === 'project') return isWorkbenchProjectSection(section) ? section : null;
  if (surface === 'pipeline') return isWorkbenchPipelineSection(section) ? section : null;
  if (surface === 'runtime') return normalizeWorkbenchRuntimeSection(section);
  return section;
}

export function normalizeWorkbenchTarget(target?: WorkbenchTargetInput | null): NormalizedWorkbenchTarget | null {
  if (!target || typeof target !== 'object') return null;
  const surface = normalizeWorkbenchSurface(target.surface);
  if (!surface) return null;

  const normalized: NormalizedWorkbenchTarget = { surface };
  for (const key of WORKBENCH_TARGET_KEYS) {
    if (key === 'section') {
      const section = normalizeWorkbenchSection(surface, target.section);
      if (target.section != null && !section) {
        if (surface === 'runtime' && compactText(target.runner_id)) continue;
        return null;
      }
      if (section) normalized.section = section;
      continue;
    }
    const value = compactText(target[key]);
    if (value) normalized[key] = value;
  }

  if (surface === 'task' && !normalized.task_id) return null;
  return normalized;
}

export function normalizeWorkbenchRefetchSurfaces(values?: readonly unknown[] | null): WorkbenchRefetchSurface[] {
  const surfaces: WorkbenchRefetchSurface[] = [];
  const seen = new Set<string>();
  for (const value of values ?? []) {
    const surface = compactText(value);
    if (!isOneOf(WORKBENCH_REFETCH_SURFACES, surface) || seen.has(surface)) continue;
    surfaces.push(surface);
    seen.add(surface);
  }
  return surfaces;
}
