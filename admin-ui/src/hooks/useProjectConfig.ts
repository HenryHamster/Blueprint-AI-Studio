/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect } from 'react';
import type {
  Project,
  AutoRemediationConfig,
  AutoRemediationStats,
  IssueImplementationConfig,
  IssueImplementationConfigUpdate,
  AutoReviewConfig,
  ProjectResourceEnvelopeResponse,
} from '../types/project';
import {
  API_BASE_URL,
  authHeaders,
  authGet,
  fetchProjectResourceEnvelope,
  updateProjectResourceEnvelope,
  type ResourceEnvelopePatch,
} from '../api';
import { resolveAccess } from '../accessControl';

/**
 * Project-scoped configuration domain: the per-project config blocks that
 * load/save against the selected project — auto-remediation, issue
 * implementation, auto-review, integration branch, and promotion target —
 * plus the derived "task branch defaults" used to prefill the new-task modal.
 *
 * The two refs (selectedProjectRepoRef, defaultTaskBranchRef) are owned by the
 * facade and shared with useTaskModal; this hook keeps them in sync with the
 * selected project and its branch config exactly as before.
 */
export function useProjectConfig(
  addToast: (msg: string, type: 'success' | 'error') => void,
  selectedProjectId: string | null,
  projects: Project[],
  selectedProjectRepoRef: React.MutableRefObject<string | null>,
  defaultTaskBranchRef: React.MutableRefObject<string | null>,
) {
  const selectedProjectRole = projects.find(project => project.id === selectedProjectId)?.my_role;
  const projectAccess = resolveAccess(undefined, selectedProjectRole);
  const canReadProjectConfig = projectAccess.canOperateProject;
  const canManageProject = projectAccess.canManageProject;

  // ── Auto-remediation config (project-scoped) ────────────────────────────
  const [autoRemediationConfig, setAutoRemediationConfig] = useState<AutoRemediationConfig | null>(null);
  const [autoRemediationStats, setAutoRemediationStats] = useState<AutoRemediationStats | null>(null);

  useEffect(() => {
    if (!selectedProjectId || !canReadProjectConfig) {
      setAutoRemediationConfig(null);
      setAutoRemediationStats(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-remediation`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.config) setAutoRemediationConfig(data.config);
        if (data?.stats) setAutoRemediationStats(data.stats);
      })
      .catch(() => {});
  }, [selectedProjectId, canReadProjectConfig]);

  const handleUpdateAutoRemediationConfig = async (config: AutoRemediationConfig) => {
    if (!selectedProjectId || !canManageProject) return;
    const prev = autoRemediationConfig;
    setAutoRemediationConfig(config);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-remediation`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify(config),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAutoRemediationConfig(data.config);
      addToast('Auto-remediation config saved', 'success');
    } catch {
      setAutoRemediationConfig(prev);
      addToast('Failed to save auto-remediation config', 'error');
    }
  };

  // ── Issue implementation config (project-scoped) ────────────────────────
  const [issueImplementationConfig, setIssueImplementationConfig] = useState<IssueImplementationConfig | null>(null);

  useEffect(() => {
    if (!selectedProjectId || !canReadProjectConfig) {
      setIssueImplementationConfig(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/issue-implementation`)
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (data?.config) setIssueImplementationConfig(data.config); })
      .catch(() => {});
  }, [selectedProjectId, canReadProjectConfig]);

  const handleUpdateIssueImplementationConfig = async (update: IssueImplementationConfigUpdate) => {
    if (!selectedProjectId || !canManageProject) return;
    const prev = issueImplementationConfig;
    // Optimistic update: preserve server-derived base_branch from prev/default.
    if (prev) setIssueImplementationConfig({ ...prev, ...update });
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/issue-implementation`, {
        method: 'PATCH',
        headers: await authHeaders(),
        // base_branch is server-derived from project.integration_branch and rejected by the PATCH validator.
        body: JSON.stringify(update),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setIssueImplementationConfig(data.config);
      addToast('Issue implementation config saved', 'success');
    } catch {
      setIssueImplementationConfig(prev);
      addToast('Failed to save issue implementation config', 'error');
    }
  };

  // ── Auto-review config (project-scoped) ─────────────────────────────────
  const [autoReviewConfig, setAutoReviewConfig] = useState<AutoReviewConfig | null>(null);
  // ── Resource envelope config (project-scoped) ───────────────────────────
  const [resourceEnvelopeData, setResourceEnvelopeData] = useState<ProjectResourceEnvelopeResponse | null>(null);
  const [resourceEnvelopeLoading, setResourceEnvelopeLoading] = useState(false);
  const [resourceEnvelopeError, setResourceEnvelopeError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedProjectId || !canReadProjectConfig) {
      setAutoReviewConfig(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-review`)
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (data?.config) setAutoReviewConfig(data.config); })
      .catch(() => {});
  }, [selectedProjectId, canReadProjectConfig]);

  useEffect(() => {
    if (!selectedProjectId || !canReadProjectConfig) {
      setResourceEnvelopeData(null);
      setResourceEnvelopeLoading(false);
      setResourceEnvelopeError(null);
      return;
    }
    setResourceEnvelopeLoading(true);
    fetchProjectResourceEnvelope(selectedProjectId)
      .then(data => {
        setResourceEnvelopeData(data);
        setResourceEnvelopeError(null);
      })
      .catch(() => {
        setResourceEnvelopeError('Failed to load resource envelope');
      })
      .finally(() => {
        setResourceEnvelopeLoading(false);
      });
  }, [selectedProjectId, canReadProjectConfig]);

  const handleUpdateAutoReviewConfig = async (patch: Partial<AutoReviewConfig>) => {
    if (!selectedProjectId || !canManageProject || Object.keys(patch).length === 0) return;
    const prev = autoReviewConfig;
    setAutoReviewConfig(prev ? { ...prev, ...patch } : prev);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/auto-review`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAutoReviewConfig(data.config);
      addToast('Auto-review config saved', 'success');
    } catch {
      setAutoReviewConfig(prev);
      addToast('Failed to save auto-review config', 'error');
    }
  };

  const handleUpdateResourceEnvelope = async (patch: ResourceEnvelopePatch) => {
    if (!selectedProjectId || !canManageProject) return;
    const prev = resourceEnvelopeData;
    if (prev) setResourceEnvelopeData({ ...prev, ...patch });
    try {
      const data = await updateProjectResourceEnvelope(selectedProjectId, patch);
      setResourceEnvelopeData(data);
      setResourceEnvelopeError(null);
      addToast('Resource envelope saved', 'success');
    } catch {
      setResourceEnvelopeData(prev);
      addToast('Failed to save resource envelope', 'error');
    }
  };

  // ── Integration branch config (project-scoped) ───────────────────────────
  const [integrationBranchData, setIntegrationBranchData] = useState<{ integration_branch: string; is_default: boolean } | null>(null);
  const [integrationBranchLoaded, setIntegrationBranchLoaded] = useState(false);
  const [integrationBranchError, setIntegrationBranchError] = useState<string | null>(null);

  // ── Promotion target config (project-scoped) ─────────────────────────────
  const [promotionTargetData, setPromotionTargetData] = useState<{ promotion_target: string; is_default: boolean } | null>(null);
  const [promotionTargetLoaded, setPromotionTargetLoaded] = useState(false);
  const [promotionTargetError, setPromotionTargetError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedProjectId || !canReadProjectConfig) {
      setIntegrationBranchData(null);
      setIntegrationBranchLoaded(false);
      setIntegrationBranchError(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/integration-branch`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.integration_branch !== undefined) setIntegrationBranchData({ integration_branch: data.integration_branch, is_default: data.is_default });
        setIntegrationBranchLoaded(true);
        setIntegrationBranchError(null);
      })
      .catch(() => {
        setIntegrationBranchLoaded(true);
        setIntegrationBranchError('Failed to load integration branch');
      });
  }, [selectedProjectId, canReadProjectConfig]);

  const handleUpdateIntegrationBranch = async (branch: string | null) => {
    if (!selectedProjectId || !canManageProject) return;
    const prev = integrationBranchData;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/integration-branch`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ integration_branch: branch }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setIntegrationBranchData({ integration_branch: data.integration_branch, is_default: data.is_default });
      addToast('Integration branch saved', 'success');
    } catch {
      setIntegrationBranchData(prev);
      addToast('Failed to save integration branch', 'error');
      throw new Error('Failed to save integration branch');
    }
  };

  useEffect(() => {
    if (!selectedProjectId || !canReadProjectConfig) {
      setPromotionTargetData(null);
      setPromotionTargetLoaded(false);
      setPromotionTargetError(null);
      return;
    }
    authGet(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/promotion-target`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.promotion_target !== undefined) setPromotionTargetData({ promotion_target: data.promotion_target, is_default: data.is_default });
        setPromotionTargetLoaded(true);
        setPromotionTargetError(null);
      })
      .catch(() => {
        setPromotionTargetLoaded(true);
        setPromotionTargetError('Failed to load promotion target');
      });
  }, [selectedProjectId, canReadProjectConfig]);

  useEffect(() => {
    const project = projects.find(p => p.id === selectedProjectId) ?? null;
    selectedProjectRepoRef.current = project ? `${project.github_owner}/${project.github_repo}` : null;
    const branchConfigReady = !selectedProjectId || !canReadProjectConfig || (
      integrationBranchLoaded &&
      promotionTargetLoaded &&
      !integrationBranchError &&
      !promotionTargetError
    );
    defaultTaskBranchRef.current = branchConfigReady
      ? promotionTargetData?.promotion_target || integrationBranchData?.integration_branch || null
      : null;
  }, [
    projects,
    selectedProjectId,
    canReadProjectConfig,
    promotionTargetData,
    integrationBranchData,
    integrationBranchLoaded,
    promotionTargetLoaded,
    integrationBranchError,
    promotionTargetError,
    selectedProjectRepoRef,
    defaultTaskBranchRef,
  ]);

  const taskBranchDefaultsReady = !selectedProjectId || !canReadProjectConfig || (
    integrationBranchLoaded &&
    promotionTargetLoaded &&
    !integrationBranchError &&
    !promotionTargetError
  );
  const taskBranchDefaultsLoading = Boolean(selectedProjectId) && !taskBranchDefaultsReady && !integrationBranchError && !promotionTargetError;
  const taskBranchDefaultsError = integrationBranchError || promotionTargetError;

  const handleUpdatePromotionTarget = async (branch: string | null) => {
    if (!selectedProjectId || !canManageProject) return;
    const prev = promotionTargetData;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/promotion-target`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ promotion_target: branch }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPromotionTargetData({ promotion_target: data.promotion_target, is_default: data.is_default });
      addToast('Promotion target saved', 'success');
    } catch {
      setPromotionTargetData(prev);
      addToast('Failed to save promotion target', 'error');
      throw new Error('Failed to save promotion target');
    }
  };

  // Approval-gate config (infra_files / critical_files) is owned and fetched by
  // ProjectDashboard.tsx via the api.ts client functions, so it intentionally
  // does NOT live in this hook — adding it here duplicated the per-project fetch
  // on every project switch for a value no consumer of this hook reads.

  return {
    autoRemediationConfig, autoRemediationStats, handleUpdateAutoRemediationConfig,
    issueImplementationConfig, handleUpdateIssueImplementationConfig,
    autoReviewConfig, handleUpdateAutoReviewConfig,
    resourceEnvelopeData, resourceEnvelopeLoading, resourceEnvelopeError, handleUpdateResourceEnvelope,
    integrationBranchData, handleUpdateIntegrationBranch,
    promotionTargetData, handleUpdatePromotionTarget,
    taskBranchDefaultsReady, taskBranchDefaultsLoading, taskBranchDefaultsError,
  };
}
