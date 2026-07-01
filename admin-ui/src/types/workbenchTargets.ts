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

export const PROJECT_TARGET_SECTIONS = [
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

export const PIPELINE_TARGET_SECTIONS = ['flow', 'board', 'archive'] as const;

export const RUNTIME_TARGET_SECTIONS = [
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
] as const;

export type WorkbenchTargetSurface = typeof WORKBENCH_TARGET_SURFACES[number];
export type WorkbenchRefetchSurface = typeof WORKBENCH_REFETCH_SURFACES[number];

export interface NormalizedWorkbenchTarget {
  surface: WorkbenchTargetSurface;
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
}

const TARGET_KEYS = [
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

const TARGET_SURFACE_SET = new Set<string>(WORKBENCH_TARGET_SURFACES);
const REFETCH_SURFACE_SET = new Set<string>(WORKBENCH_REFETCH_SURFACES);
const TARGET_SECTIONS_BY_SURFACE: Partial<Record<WorkbenchTargetSurface, Set<string>>> = {
  project: new Set(PROJECT_TARGET_SECTIONS),
  pipeline: new Set(PIPELINE_TARGET_SECTIONS),
  runtime: new Set(RUNTIME_TARGET_SECTIONS),
};
const TARGET_SECTION_ALIASES_BY_SURFACE: Partial<Record<WorkbenchTargetSurface, Record<string, string>>> = {
  runtime: {
    runners: 'devices',
  },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function compactString(value: unknown): string | null {
  if (value == null) return null;
  const text = String(value).trim().replace(/\s+/g, ' ');
  return text || null;
}

export function isWorkbenchTargetSurface(value: unknown): value is WorkbenchTargetSurface {
  return typeof value === 'string' && TARGET_SURFACE_SET.has(value);
}

export function isWorkbenchRefetchSurface(value: unknown): value is WorkbenchRefetchSurface {
  return typeof value === 'string' && REFETCH_SURFACE_SET.has(value);
}

export function normalizeWorkbenchTarget(target: unknown): NormalizedWorkbenchTarget | null {
  if (!isRecord(target)) return null;
  const surface = compactString(target.surface);
  if (!isWorkbenchTargetSurface(surface)) return null;

  const normalized: NormalizedWorkbenchTarget = { surface };
  const validSections = TARGET_SECTIONS_BY_SURFACE[surface] ?? null;
  const sectionAliases = TARGET_SECTION_ALIASES_BY_SURFACE[surface] ?? {};
  TARGET_KEYS.forEach(key => {
    let value = compactString(target[key]);
    if (!value) return;
    if (key === 'section') value = sectionAliases[value] ?? value;
    if (key === 'section' && validSections && !validSections.has(value)) return;
    (normalized as unknown as Record<string, string | null>)[key] = value;
  });
  return normalized;
}

export function normalizeWorkbenchRefetchSurfaces(surfaces: unknown): WorkbenchRefetchSurface[] {
  if (!Array.isArray(surfaces)) return [];
  const seen = new Set<WorkbenchRefetchSurface>();
  const normalized: WorkbenchRefetchSurface[] = [];
  surfaces.forEach(surface => {
    if (!isWorkbenchRefetchSurface(surface) || seen.has(surface)) return;
    seen.add(surface);
    normalized.push(surface);
  });
  return normalized;
}
