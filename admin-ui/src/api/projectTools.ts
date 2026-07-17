import { apiFetch, API_BASE_URL } from './core';

export interface ProjectTool {
  id: string;
  kind: string;
  slug: string;
  title: string;
  enabled: boolean;
  placement: string;
  content_revision: number;
  config?: Record<string, unknown>;
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
  placement?: string;
}

/** Placements the backend accepts (mirrors _VALID_PLACEMENTS server-side). */
export const TOOL_PLACEMENTS = ['project_section', 'toolbar', 'sidebar'] as const;

export interface FetchProjectToolsResult {
  tools: ProjectTool[];
  total_count: number;
  next_offset: number | null;
}

export async function fetchProjectTools(
  projectId: string,
  options?: { limit?: number; offset?: number },
): Promise<FetchProjectToolsResult> {
  const params = new URLSearchParams();
  if (options?.limit !== undefined) params.set('limit', String(options.limit));
  if (options?.offset !== undefined) params.set('offset', String(options.offset));
  const qs = params.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/tools${qs ? `?${qs}` : ''}`;
  const res = await apiFetch(url);
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

export interface ResidentToolArtifactRef {
  id: string;
  artifact_ref_id: string;
  tool_id: string;
  project_id: string;
  kind: 'notes' | 'whiteboard';
  revision: number;
  checksum: string;
  author: string;
  timestamp: string;
  retrieval: {
    capability: 'inline_bounded_text' | 'project_tool_rendered_image';
    href: string;
  };
}

export interface ResidentRenderedImageReceipt {
  attachment_id: string;
  storage_uri: string;
  checksum: string;
  mime_type?: string;
  width?: number;
  height?: number;
}

export interface ResidentToolArtifactReadyResponse {
  state: 'ready';
  artifact_ref: ResidentToolArtifactRef;
}

export interface ResidentToolArtifactPendingResponse {
  state: 'pending';
  promotion_intent_id: string;
  receipt_id: string;
  retry_after_ms: number;
}

export type ResidentToolArtifactCreateResponse =
  | ResidentToolArtifactReadyResponse
  | ResidentToolArtifactPendingResponse;

export class ToolArtifactRequestError extends Error {
  readonly status: number | null;
  readonly retryable: boolean;

  constructor(message: string, status: number | null, retryable: boolean) {
    super(message);
    this.name = 'ToolArtifactRequestError';
    this.status = status;
    this.retryable = retryable;
  }
}

export class ToolArtifactConflictError extends Error {
  readonly currentRevision: number | null;
  readonly action: string;

  constructor(currentRevision: number | null, action = 'reload_or_rebase') {
    super('Tool changed before it could be shared. Reload or rebase your edits.');
    this.name = 'ToolArtifactConflictError';
    this.currentRevision = currentRevision;
    this.action = action;
  }
}

export async function createResidentToolArtifactRef(
  projectId: string,
  toolId: string,
  expectedRevision: number,
  renderedImage?: ResidentRenderedImageReceipt,
): Promise<ResidentToolArtifactCreateResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/tools/${encodeURIComponent(toolId)}/artifact-refs`,
    {
      method: 'POST',
      body: JSON.stringify({
        expected_revision: expectedRevision,
        ...(renderedImage ? { rendered_image: renderedImage } : {}),
      }),
    },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as {
      detail?: string | { current_revision?: number; action?: string };
      error?: { message?: string | { current_revision?: number; action?: string } };
      current_revision?: number;
      action?: string;
    };
    if (res.status === 409) {
      const wrapped = body.error?.message;
      const detail = typeof body.detail === 'object' && body.detail
        ? body.detail
        : typeof wrapped === 'object' && wrapped
          ? wrapped
          : body;
      throw new ToolArtifactConflictError(
        typeof detail.current_revision === 'number' ? detail.current_revision : null,
        typeof detail.action === 'string' ? detail.action : undefined,
      );
    }
    const detail = typeof body.detail === 'string'
      ? body.detail
      : typeof body.error?.message === 'string'
        ? body.error.message
        : `Failed to share tool revision: ${res.status}`;
    throw new ToolArtifactRequestError(
      detail,
      res.status,
      res.status === 401
        || res.status === 408
        || res.status === 425
        || res.status === 429
        || res.status >= 500,
    );
  }
  const body = await res.json() as Partial<ResidentToolArtifactCreateResponse>;
  if (body.state === 'ready' && body.artifact_ref) {
    return { state: 'ready', artifact_ref: body.artifact_ref };
  }
  if (
    body.state === 'pending'
    && typeof body.promotion_intent_id === 'string'
    && body.promotion_intent_id.length > 0
    && typeof body.receipt_id === 'string'
    && body.receipt_id.length > 0
    && typeof body.retry_after_ms === 'number'
    && Number.isFinite(body.retry_after_ms)
    && body.retry_after_ms >= 0
  ) {
    return {
      state: 'pending',
      promotion_intent_id: body.promotion_intent_id,
      receipt_id: body.receipt_id,
      retry_after_ms: body.retry_after_ms,
    };
  }
  throw new ToolArtifactRequestError(
    'Artifact reference response was not recognized.',
    res.status,
    false,
  );
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

export interface ProjectSkillManifestEntry {
  slug: string;
  title: string;
  command: string;
  description?: string;
}

/**
 * Fetch tools and skills declared in the project's `.blueprint/tools.json` manifest.
 * Returns manifest tool entries, skill entries, and an optional error string
 * (set for partial validation failures; null means no issues).
 */
export async function fetchProjectToolManifest(
  projectId: string,
  signal?: AbortSignal,
): Promise<{ tools: ProjectToolManifestEntry[]; skills: ProjectSkillManifestEntry[]; error: string | null }> {
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
 * point with the capability token embedded in the URL path.
 *
 * The token is placed in the path rather than as a query parameter so that
 * relative subresources (e.g. `<script src="./app.js">`) resolve inside the
 * token-prefix tree and authenticate automatically.  A query-string token
 * would be dropped on relative resolution, causing 401 for every subresource
 * in multi-file tools inside a `sandbox="allow-scripts"` (no
 * `allow-same-origin`) iframe where cookies and custom headers are unavailable.
 *
 * Route shape: `/api/admin/projects/{id}/tools/{slug}/t/{token}/assets/{path}`
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
  return `${base}/api/admin/projects/${proj}/tools/${slug}/t/${encodeURIComponent(token)}/assets/${encodedPath}`;
}

// ── Compute image status (owner-only baked-image read surface) ──────────────

export interface ComputeImageDrift {
  drifted: boolean | null;
  current_dep_hash?: string;
  stored_dep_hash?: string;
  reason?: string;
}

export interface ComputeImageBuiltStatus {
  status: 'built';
  pinned_ref: string;
  digest: string | null;
  dep_hash: string;
  tag: string;
  built: boolean;
  created_at: string | null;
  updated_at: string | null;
  is_digest_pinned: boolean;
  drift?: ComputeImageDrift;
}

export interface ComputeImageNoDeclareStatus {
  status: 'no_compute_image_declared';
}

export interface ComputeImageNeverBuiltStatus {
  status: 'never_built';
}

export type ComputeImageStatusResponse =
  | ComputeImageBuiltStatus
  | ComputeImageNoDeclareStatus
  | ComputeImageNeverBuiltStatus;

/**
 * Fetch the baked compute-image build record for a project.
 *
 * When ``checkDrift`` is true the backend clones the repo and computes the
 * current dependency hash (expensive) to report whether it has drifted from
 * the stored build record.  Omit ``checkDrift`` (or pass false) for the cheap
 * status-only read.
 */
export async function fetchComputeImageStatus(
  projectId: string,
  checkDrift?: boolean,
): Promise<ComputeImageStatusResponse> {
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/compute-image/status${checkDrift ? '?check_drift=true' : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch compute image status: ${res.status}`);
  return res.json();
}
