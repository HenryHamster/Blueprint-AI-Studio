import { apiFetch, API_BASE_URL } from './core';

export interface ProjectTool {
  id: string;
  kind: string;
  slug: string;
  title: string;
  enabled: boolean;
  placement: string;
  content_revision: number;
}

export interface ToolKindConfig {
  kind: string;
  label: string;
  description: string;
  icon?: string;
  config_schema?: Record<string, unknown>;
}

export interface CreateProjectToolPayload {
  kind: string;
  title: string;
  config?: Record<string, unknown>;
}

export interface UpdateProjectToolPayload {
  title?: string;
  config?: Record<string, unknown>;
  enabled?: boolean;
}

export async function fetchProjectTools(projectId: string): Promise<ProjectTool[]> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/tools`);
  if (!res.ok) throw new Error(`Failed to fetch project tools: ${res.status}`);
  const data = await res.json();
  return data.tools;
}

export async function createProjectTool(
  projectId: string,
  payload: CreateProjectToolPayload,
): Promise<ProjectTool> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/tools`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to create project tool: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to create project tool: ${res.status}`);
  }
  return res.json();
}

export async function updateProjectTool(
  projectId: string,
  toolId: string,
  payload: UpdateProjectToolPayload,
): Promise<ProjectTool> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/tools/${encodeURIComponent(toolId)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(payload),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update project tool: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update project tool: ${res.status}`);
  }
  return res.json();
}

export async function deleteProjectTool(projectId: string, toolId: string): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/tools/${encodeURIComponent(toolId)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to delete project tool: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to delete project tool: ${res.status}`);
  }
}

export async function seedProjectTools(projectId: string): Promise<{ status: string; added: string[]; skipped: string[] }> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/tools/seed`, {
    method: 'POST',
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to seed project tools: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to seed project tools: ${res.status}`);
  }
  return res.json();
}

export async function fetchToolKinds(): Promise<ToolKindConfig[]> {
  const res = await apiFetch('/api/admin/tool-kinds');
  if (!res.ok) throw new Error(`Failed to fetch tool kinds: ${res.status}`);
  return res.json();
}

export interface ToolContent<T = Record<string, unknown>> {
  content: T;
  content_revision: number;
}

export interface ToolContentSaveResult {
  content_revision: number;
}

export async function fetchToolContent<T = Record<string, unknown>>(
  projectId: string,
  toolId: string,
): Promise<ToolContent<T>> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/tools/${encodeURIComponent(toolId)}/content`,
  );
  if (!res.ok) throw new Error(`Failed to fetch tool content: ${res.status}`);
  return res.json();
}

export class ConflictError extends Error {
  constructor() {
    super('Save conflict — content was modified by another writer.');
    this.name = 'ConflictError';
  }
}

export async function saveToolContent(
  projectId: string,
  toolId: string,
  content: Record<string, unknown>,
  baseRevision: number,
): Promise<ToolContentSaveResult> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/tools/${encodeURIComponent(toolId)}/content`,
    {
      method: 'PUT',
      body: JSON.stringify({ content, base_revision: baseRevision }),
    },
  );
  if (!res.ok) {
    if (res.status === 409) {
      throw new ConflictError();
    }
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to save tool content: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to save tool content: ${res.status}`);
  }
  return res.json();
}

// ── Project manifest (repo-declared tool discovery) ────────────────────────

export interface ProjectToolManifestEntry {
  slug: string;
  title: string;
  description?: string;
  placement: string;
  entry: string;
  kind: 'project';
  source: 'project';
}

/**
 * Fetch tools declared in the project's `.blueprint/tools.json` manifest.
 * Returns a list of manifest tool entries plus an optional error string
 * (set for partial validation failures; null means no issues).
 */
export async function fetchProjectToolManifest(
  projectId: string,
  signal?: AbortSignal,
): Promise<{ tools: ProjectToolManifestEntry[]; error: string | null }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/tools/manifest`,
    undefined, 2, 30000, signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch project tool manifest: ${res.status}`);
  return res.json();
}

/**
 * Mint a short-lived capability JWT for accessing a project-declared tool's
 * assets (HTML, JS, CSS, etc.). The token is passed as a query parameter
 * to the asset-serving endpoint since opaque-origin iframes cannot send
 * cookies or Authorization headers.
 */
export async function fetchAssetToken(
  projectId: string,
  toolSlug: string,
  signal?: AbortSignal,
): Promise<{ token: string; expires_at: string }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/tools/${encodeURIComponent(toolSlug)}/asset-token`,
    { method: 'POST' },
    2, 30000, signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch asset token: ${res.status}`);
  return res.json();
}

/**
 * Build the absolute URL for loading a project-declared tool's asset entry
 * point with the capability token as a query parameter.
 *
 * The entry path is a relative path like "index.html" or "dist/index.html"
 * within the tool's `.blueprint/tools/{toolSlug}/` directory. Path separators
 * are preserved so multi-segment paths work with the FastAPI `{path:path}`
 * route parameter.
 */
export function buildToolAssetUrl(
  projectId: string,
  toolSlug: string,
  entryPath: string,
  token: string,
): string {
  const base = API_BASE_URL || '';
  const proj = encodeURIComponent(projectId);
  const slug = encodeURIComponent(toolSlug);
  const encodedPath = entryPath.split('/').map(seg => encodeURIComponent(seg)).join('/');
  return `${base}/api/admin/projects/${proj}/tools/${slug}/assets/${encodedPath}?token=${encodeURIComponent(token)}`;
}
