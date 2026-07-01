import { normalizeWorkbenchRefetchSurfaces } from './workbenchTargets';

export interface WorkbenchEventEnvelopeV1 {
  schema_version: 1;
  project_id: string;
  resource: string;
  action: string;
  refetch?: {
    surfaces?: string[];
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

export function isWorkbenchEventEnvelopeV1(value: unknown): value is WorkbenchEventEnvelopeV1 {
  if (!isObject(value)) return false;
  if (value.schema_version !== 1) return false;
  return (
    typeof value.project_id === 'string'
    && typeof value.resource === 'string'
    && typeof value.action === 'string'
  );
}

export function workbenchRefetchSurfacesFromData(
  data: unknown,
  expectedProjectId?: string | null,
): Set<string> {
  if (!isObject(data)) return new Set();
  if (!workbenchEventDataMatchesProject(data, expectedProjectId)) {
    return new Set();
  }
  const refetch = isObject(data.refetch) ? data.refetch : null;
  return new Set(normalizeWorkbenchRefetchSurfaces(refetch?.surfaces));
}

export function workbenchRefetchSurfacesFromMessage(
  message: unknown,
  expectedProjectId?: string | null,
): Set<string> {
  const data = isObject(message) ? message.data : null;
  return workbenchRefetchSurfacesFromData(data, expectedProjectId);
}

export function workbenchEventDataMatchesProject(
  data: unknown,
  expectedProjectId?: string | null,
): boolean {
  if (!expectedProjectId || !isWorkbenchEventEnvelopeV1(data)) return true;
  return data.project_id === expectedProjectId;
}

export function workbenchMessageMatchesProject(
  message: unknown,
  expectedProjectId?: string | null,
): boolean {
  const data = isObject(message) ? message.data : null;
  return workbenchEventDataMatchesProject(data, expectedProjectId);
}
