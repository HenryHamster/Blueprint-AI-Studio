import { apiFetch } from './core';

export interface ProjectTool {
  id: string;
  project_id: string;
  kind: string;
  label: string;
  config: Record<string, unknown>;
  enabled: boolean;
  created_at: string;
  updated_at: string;
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
  label: string;
  config?: Record<string, unknown>;
}

export interface UpdateProjectToolPayload {
  label?: string;
  config?: Record<string, unknown>;
  enabled?: boolean;
}

export async function fetchProjectTools(projectId: string): Promise<ProjectTool[]> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/tools`);
  if (!res.ok) throw new Error(`Failed to fetch project tools: ${res.status}`);
  return res.json();
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

export async function fetchToolKinds(): Promise<ToolKindConfig[]> {
  const res = await apiFetch('/api/admin/tool-kinds');
  if (!res.ok) throw new Error(`Failed to fetch tool kinds: ${res.status}`);
  return res.json();
}
