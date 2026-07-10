import { apiFetch } from './core';
import type { Alert, AlertFilters, ProjectAlertDomainsResponse } from '../types/alert';

export async function fetchAlerts(
  filters: AlertFilters = {},
  signal?: AbortSignal,
): Promise<Alert[]> {
  const params = new URLSearchParams();
  if (filters.projectId) params.set('project_id', filters.projectId);
  if (filters.domain) params.set('domain', filters.domain);
  if (filters.eventType) params.set('event_type', filters.eventType);
  if (filters.source) params.set('source', filters.source);
  const query = params.toString();
  const res = await apiFetch(`/api/admin/alerts${query ? `?${query}` : ''}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch alerts: ${res.status}`);
  return res.json();
}

export async function fetchProjectAlertDomains(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectAlertDomainsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/alert-domains`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch alert domains: ${res.status}`);
  return res.json();
}

export async function updateProjectAlertDomains(
  projectId: string,
  domains: Record<string, { enabled: boolean } | null>,
): Promise<ProjectAlertDomainsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/alert-domains`,
    {
      method: 'PATCH',
      body: JSON.stringify({ domains }),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update alert domains: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update alert domains: ${res.status}`);
  }
  return res.json();
}

/** The closed set of comparison operators a compute success criterion may use.
 * Mirrors the backend `ComputeSuccessCriterion.operator` Literal verbatim. */

export interface CompletionDebtEntry {
  id: string;
  project_id: string;
  kind: string;
  title: string;
  detail: string | null;
  status: 'open' | 'dismissed' | 'resolved';
  dismiss_reason: string | null;
  created_at: string;
  dismissed_at: string | null;
  resolved_at: string | null;
}

/** List completion-debt entries for a project. */
export async function fetchCompletionDebts(
  projectId: string,
  filters?: { status?: string; kind?: string },
): Promise<{ entries: CompletionDebtEntry[]; count: number; total_count: number }> {
  const params = new URLSearchParams();
  if (filters?.status) params.set('status', filters.status);
  if (filters?.kind) params.set('kind', filters.kind);
  const qs = params.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/completion-debts${qs ? `?${qs}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to fetch completion debts: ${res.status}`);
  }
  return res.json();
}

/** Dismiss a completion-debt entry with a reason. */
export async function dismissCompletionDebt(
  projectId: string,
  debtId: string,
  reason: string,
): Promise<{ status: string; entry: CompletionDebtEntry }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/completion-debts/${encodeURIComponent(debtId)}/dismiss`,
    { method: 'POST', body: JSON.stringify({ reason }), headers: { 'Content-Type': 'application/json' } },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to dismiss completion debt: ${res.status}`);
  }
  return res.json();
}

/** Resolve a completion-debt entry (mark work complete). */
export async function resolveCompletionDebt(
  projectId: string,
  debtId: string,
): Promise<{ status: string; entry: CompletionDebtEntry }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/completion-debts/${encodeURIComponent(debtId)}/resolve`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to resolve completion debt: ${res.status}`);
  }
  return res.json();
}

/** A dead-lettered notification that exhausted retries or was evicted from the queue. */
export interface DeadLetterEntry {
  id: string;
  project_id: string;
  event_type: string;
  reason: string;
  attempts: number;
  last_status: number | null;
  webhook_url: string;
  payload: Record<string, unknown>;
  body: Record<string, unknown>;
  created_at: string;
}

/** List dead-lettered notifications for a project (newest first). */
export async function fetchDeadLetters(
  projectId: string,
  signal?: AbortSignal,
  limit = 100,
  offset = 0,
): Promise<{ items: DeadLetterEntry[]; total: number; limit: number; offset: number }> {
  const params = new URLSearchParams({ project_id: projectId, limit: String(limit), offset: String(offset) });
  const res = await apiFetch(
    `/api/admin/notification-dispatch/dead-letters?${params}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to fetch dead letters: ${res.status}`);
  }
  return res.json();
}

/** Re-drive one or more dead-lettered notifications through the retry queue. */
export async function redriveDeadLetters(
  projectId: string,
  ids: string[],
): Promise<{ redriven: number; errors: Array<{ id: string; error: string }> }> {
  const res = await apiFetch(
    '/api/admin/notification-dispatch/dead-letters/redrive',
    {
      method: 'POST',
      body: JSON.stringify({ project_id: projectId, ids }),
      headers: { 'Content-Type': 'application/json' },
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to redrive dead letters: ${res.status}`);
  }
  return res.json();
}
