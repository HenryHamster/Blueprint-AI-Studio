/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useRef, useState } from 'react';
import { API_BASE_URL, apiFetch } from '../api';
import { apiErrorMessage } from '../api/core';
import { DEFAULT_POLL_MS, usePoller } from './usePoller';

export const PROJECT_RESOURCE_SUMMARY_POLL_MS = DEFAULT_POLL_MS;

export interface ProjectResourceSummary {
  project_id: string;
  policy: {
    execution_resources: 'project owner';
    attribution: 'launcher';
    managed_by: 'owner';
  };
  accounts: {
    total: number;
    enabled: number;
    enabled_below_cap: number;
    at_cap: number;
    project_active_tasks: number;
    project_daily_task_count: number;
    project_daily_task_cap: number | null;
    project_max_concurrent_tasks: number | null;
  };
  runners: {
    total: number;
    online: number;
    offline: number;
  };
}

interface ResourceSummaryState {
  projectId: string | null;
  data: ProjectResourceSummary | null;
  loading: boolean;
  error: string | null;
}

const EMPTY_STATE: ResourceSummaryState = {
  projectId: null,
  data: null,
  loading: false,
  error: null,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function requiredCount(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new Error('Invalid project resource summary');
  }
  return value;
}

function requiredNullableCount(record: Record<string, unknown>, key: string): number | null {
  return record[key] === null ? null : requiredCount(record, key);
}

function parseSummary(payload: unknown, requestedProjectId: string): ProjectResourceSummary {
  if (!isRecord(payload)) throw new Error('Invalid project resource summary');
  const body = payload;
  if (body.project_id !== requestedProjectId) throw new Error('Invalid project resource summary');
  if (!isRecord(body.policy) || !isRecord(body.accounts) || !isRecord(body.runners)) {
    throw new Error('Invalid project resource summary');
  }
  const policy = body.policy;
  const accounts = body.accounts;
  const runners = body.runners;
  if (
    policy.execution_resources !== 'project owner'
    || policy.attribution !== 'launcher'
    || policy.managed_by !== 'owner'
  ) {
    throw new Error('Invalid project resource summary');
  }

  const accountCounts = {
    total: requiredCount(accounts, 'total'),
    enabled: requiredCount(accounts, 'enabled'),
    enabled_below_cap: requiredCount(accounts, 'enabled_below_cap'),
    at_cap: requiredCount(accounts, 'at_cap'),
    project_active_tasks: requiredCount(accounts, 'project_active_tasks'),
    project_daily_task_count: requiredCount(accounts, 'project_daily_task_count'),
    project_daily_task_cap: requiredNullableCount(accounts, 'project_daily_task_cap'),
    project_max_concurrent_tasks: requiredNullableCount(accounts, 'project_max_concurrent_tasks'),
  };
  const runnerCounts = {
    total: requiredCount(runners, 'total'),
    online: requiredCount(runners, 'online'),
    offline: requiredCount(runners, 'offline'),
  };
  if (
    accountCounts.enabled > accountCounts.total
    || accountCounts.enabled_below_cap + accountCounts.at_cap !== accountCounts.enabled
    || runnerCounts.online + runnerCounts.offline !== runnerCounts.total
  ) {
    throw new Error('Invalid project resource summary');
  }

  return {
    project_id: requestedProjectId,
    policy: {
      execution_resources: 'project owner',
      attribution: 'launcher',
      managed_by: 'owner',
    },
    accounts: accountCounts,
    runners: runnerCounts,
  };
}

/** Project-scoped, aggregate-only resource availability with an A→B response fence. */
export function useProjectResourceSummary(projectId: string | null, enabled: boolean) {
  const [state, setState] = useState<ResourceSummaryState>(EMPTY_STATE);
  const scopeRef = useRef({ projectId, enabled, generation: 0 });

  useEffect(() => {
    const generation = scopeRef.current.generation + 1;
    scopeRef.current = { projectId, enabled, generation };

    if (!projectId || !enabled) {
      setState(EMPTY_STATE);
    } else {
      setState({ projectId, data: null, loading: true, error: null });
    }
    return () => {
      scopeRef.current = {
        projectId: null,
        enabled: false,
        generation: scopeRef.current.generation + 1,
      };
    };
  }, [enabled, projectId]);

  usePoller(async (signal) => {
    if (!projectId || !enabled || signal.aborted) return;
    const requestedProjectId = projectId;
    const generation = scopeRef.current.generation + 1;
    scopeRef.current = { projectId: requestedProjectId, enabled: true, generation };
    try {
      const response = await apiFetch(
        `${API_BASE_URL}/api/admin/projects/${encodeURIComponent(requestedProjectId)}/resource-summary`,
        { method: 'GET' },
        2,
        30_000,
        signal,
        false,
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(apiErrorMessage(payload, 'Could not load project resource availability'));
      }
      const data = parseSummary(await response.json(), requestedProjectId);
      if (
        !signal.aborted
        && scopeRef.current.enabled
        && scopeRef.current.projectId === requestedProjectId
        && scopeRef.current.generation === generation
      ) {
        setState({ projectId: requestedProjectId, data, loading: false, error: null });
      }
    } catch (reason) {
      if (
        !signal.aborted
        && scopeRef.current.enabled
        && scopeRef.current.projectId === requestedProjectId
        && scopeRef.current.generation === generation
      ) {
        setState({
          projectId: requestedProjectId,
          data: null,
          loading: false,
          error: reason instanceof Error ? reason.message : 'Could not load project resource availability',
        });
      }
    }
  }, {
    intervalMs: PROJECT_RESOURCE_SUMMARY_POLL_MS,
    enabled: Boolean(projectId) && enabled,
    deps: [projectId, enabled],
  });

  if (!enabled || !projectId || state.projectId !== projectId) return EMPTY_STATE;
  return state;
}
