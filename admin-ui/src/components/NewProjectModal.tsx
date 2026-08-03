/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect } from 'react';
import { X, GitBranch, Loader2, CheckCircle, Plus, Search, ArrowRight, Wrench, Zap, Tag, AlertTriangle, Github } from 'lucide-react';
import { onboardProject, previewOnboardProject, authGet, apiFetch, API_BASE_URL } from '../api';
import { apiErrorMessage } from '../api/core';
import type { BuildConfig, BuildConfigDirectory, AutoRemediationConfig, MergeVerificationVerdict } from '../types/project';
import type { ProblemModelPatch } from '../types';
import { MergeVerificationBanner } from './MergeVerificationBanner';
import { useGitHubConnection } from '../hooks/useGitHubConnection';
import {
  clearNewProjectOAuthResume,
  readNewProjectOAuthResume,
  saveNewProjectOAuthResume,
} from '../utils/newProjectOAuthResume';

interface IssueImplementationConfig {
  enabled: boolean;
  trigger_label: string;
  max_per_hour: number;
  max_concurrent: number;
  base_branch: string;
  default_effort: string;
}

type Step = 'repo' | 'config' | 'submitting' | 'success';

interface RepoOption {
  full_name: string;
  default_branch: string;
}

interface InitialSteeringState {
  projectType: string;
  users: string;
  team: string;
  deployCadence: string;
  notes: string;
  constraints: string[];
}

interface SetupProject {
  id: string;
  owner: string;
  repo: string;
}

interface NewProjectModalProps {
  onClose: () => void;
  onSuccess: (projectId: string) => void;
  /** When provided, skip the wizard and show the setup guide for an existing project. */
  setupProject?: SetupProject;
  /** Restore the narrow, credential-free form snapshot saved before GitHub OAuth. */
  resumeFromGitHub?: boolean;
}

function parseRepoInput(input: string): { owner: string; repo: string } | null {
  const trimmed = input.trim();
  const urlMatch = trimmed.match(/github\.com\/([^/]+)\/([^/\s]+?)(?:\.git)?(?:\/.*)?$/);
  if (urlMatch) return { owner: urlMatch[1], repo: urlMatch[2] };
  const shortMatch = trimmed.match(/^([a-zA-Z0-9_.-]+)\/([a-zA-Z0-9_.-]+)$/);
  if (shortMatch) return { owner: shortMatch[1], repo: shortMatch[2] };
  return null;
}

function buildInitialSteeringPayload(
  state: InitialSteeringState,
): Pick<ProblemModelPatch, 'context' | 'constraints'> | undefined {
  const contextEntries = [
    ['project_type', state.projectType],
    ['users', state.users],
    ['team', state.team],
    ['deploy_cadence', state.deployCadence],
    ['notes', state.notes],
  ]
    .map(([key, value]) => [key, value.trim()] as const)
    .filter(([, value]) => value.length > 0);

  const context = Object.fromEntries(contextEntries);
  const constraints = state.constraints.map((constraint) => constraint.trim()).filter((constraint) => constraint.length > 0);

  const payload: Pick<ProblemModelPatch, 'context' | 'constraints'> = {};
  if (Object.keys(context).length > 0) payload.context = context;
  if (constraints.length > 0) payload.constraints = constraints;
  return Object.keys(payload).length > 0 ? payload : undefined;
}

function cloneBuildConfig(config: BuildConfig | null): BuildConfig | null {
  if (!config) return null;
  return {
    ...config,
    directories: (config.directories ?? []).map((directory) => ({ ...directory })),
    env_vars: config.env_vars ? { ...config.env_vars } : {},
  };
}

function newBuildConfigDirectory(): BuildConfigDirectory {
  return {
    path: '',
    type: 'other',
    lint_cmd: '',
    build_cmd: '',
    test_cmd: '',
    install_cmd: '',
  };
}

function deriveMergeVerificationVerdict(config: BuildConfig | null): MergeVerificationVerdict | null {
  if (!config) return null;
  if (config.requires_ci_checks !== false) {
    return { state: 'ci_checks', test_commands: [] };
  }
  const testCommands = (config.directories ?? [])
    .map((directory) => {
      const testCmd = directory.test_cmd?.trim() ?? '';
      return testCmd ? { path: directory.path ?? '', test_cmd: testCmd } : null;
    })
    .filter((entry): entry is { path: string; test_cmd: string } => entry !== null);
  return testCommands.length > 0
    ? { state: 'tests_gated', test_commands: testCommands }
    : { state: 'unverified', test_commands: [] };
}

function installErrorBannerContent(error: string, owner?: string, repo?: string) {
  const normalized = error.toLowerCase();
  const isInstallationError = normalized.includes('installation token')
    || (normalized.includes('github app') && normalized.includes('not installed'));
  if (!isInstallationError) return null;
  const repoRef = owner && repo ? `${owner}/${repo}` : 'the target repository';
  return (
    <>
      <p>The Blueprint GitHub App isn't installed on {repoRef} yet. Install it on the repo (or the org, scoped to include this repo), then try again.</p>
      <p className="mt-1 opacity-60">{error}</p>
    </>
  );
}

const STEP_LABELS: Record<string, string> = { repo: 'Repository', config: 'Confirm', success: 'Created' };

export function NewProjectModal({ onClose, onSuccess, setupProject, resumeFromGitHub = false }: NewProjectModalProps) {
  const [oauthResume] = useState(() => resumeFromGitHub ? readNewProjectOAuthResume() : null);
  const githubConnection = useGitHubConnection(!setupProject);
  const [step, setStep] = useState<Step>(setupProject ? 'success' : 'repo');
  const [repoInput, setRepoInput] = useState(oauthResume?.repoInput ?? '');
  const [projectName, setProjectName] = useState(oauthResume?.projectName ?? '');
  const [goal, setGoal] = useState(oauthResume?.goal ?? '');
  const [projectType, setProjectType] = useState(oauthResume?.projectType ?? '');
  const [users, setUsers] = useState(oauthResume?.users ?? '');
  const [team, setTeam] = useState(oauthResume?.team ?? '');
  const [deployCadence, setDeployCadence] = useState(oauthResume?.deployCadence ?? '');
  const [notes, setNotes] = useState(oauthResume?.notes ?? '');
  const [constraints, setConstraints] = useState<string[]>(oauthResume?.constraints ?? ['']);
  const [repoError, setRepoError] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [createdProjectId, setCreatedProjectId] = useState(setupProject?.id ?? '');
  const [detectedConfig, setDetectedConfig] = useState<BuildConfig | null>(null);
  const [detectedVerdict, setDetectedVerdict] = useState<MergeVerificationVerdict | null>(null);
  const [buildConfigDraft, setBuildConfigDraft] = useState<BuildConfig | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');

  // Repo picker state
  const [detectedDefaultBranch, setDetectedDefaultBranch] = useState('');
  const [previewDefaultBranch, setPreviewDefaultBranch] = useState(oauthResume?.previewDefaultBranch ?? '');
  const [promotionTargetInput, setPromotionTargetInput] = useState(oauthResume?.promotionTargetInput ?? '');

  // Feature configs for the post-install setup guide
  const [autoRemediationConfig, setAutoRemediationConfig] = useState<AutoRemediationConfig | null>(null);
  const [issueImplementationConfig, setIssueImplementationConfig] = useState<IssueImplementationConfig | null>(null);
  const [featureConfigLoading, setFeatureConfigLoading] = useState(false);
  const [autoRemSaving, setAutoRemSaving] = useState(false);
  const [issueImplSaving, setIssueImplSaving] = useState(false);
  const [autoRemToggleError, setAutoRemToggleError] = useState('');
  const [issueImplToggleError, setIssueImplToggleError] = useState('');

  // Repo picker state
  const [repoOptions, setRepoOptions] = useState<RepoOption[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoFetchError, setRepoFetchError] = useState<string | null>(null);
  const [useManualRepo, setUseManualRepo] = useState(oauthResume?.useManualRepo ?? false);
  const [repoFilter, setRepoFilter] = useState(oauthResume?.repoFilter ?? '');

  useEffect(() => {
    if (oauthResume) clearNewProjectOAuthResume();
  }, [oauthResume]);

  useEffect(() => {
    let cancelled = false;
    if (setupProject || githubConnection.loading || !githubConnection.status?.identity_linked) {
      setRepoLoading(false);
      setRepoOptions([]);
      setRepoFetchError(null);
      return () => { cancelled = true; };
    }
    setRepoLoading(true);
    setRepoFetchError(null);
    authGet(`${API_BASE_URL}/api/admin/github/repos`)
      .then(async res => {
        if (cancelled) return;
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          if (cancelled) return;
          setRepoFetchError(apiErrorMessage(errData, 'Failed to load repositories'));
          setUseManualRepo(true);
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        const repos: RepoOption[] = data.repositories || [];
        setRepoOptions(repos);
        if (repos.length === 0) {
          setRepoFetchError('No repositories found for this GitHub installation');
          setUseManualRepo(true);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setRepoFetchError('Failed to connect to server');
        setUseManualRepo(true);
      })
      .finally(() => {
        if (!cancelled) setRepoLoading(false);
      });
    return () => { cancelled = true; };
  }, [setupProject, githubConnection.loading, githubConnection.status?.identity_linked]);

  // When opened in setup-guide mode for an existing project, load its feature configs
  useEffect(() => {
    if (!setupProject) return;
    setFeatureConfigLoading(true);
    Promise.all([
      apiFetch(`/api/admin/projects/${encodeURIComponent(setupProject.id)}/auto-remediation`),
      apiFetch(`/api/admin/projects/${encodeURIComponent(setupProject.id)}/issue-implementation`),
    ]).then(async ([arRes, iiRes]) => {
      if (arRes.ok) {
        const arData = await arRes.json();
        if (arData?.config) setAutoRemediationConfig(arData.config as AutoRemediationConfig);
      }
      if (iiRes.ok) {
        const iiData = await iiRes.json();
        if (iiData?.config) setIssueImplementationConfig(iiData.config as IssueImplementationConfig);
      }
    }).catch(() => {}).finally(() => setFeatureConfigLoading(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const parsed = parseRepoInput(repoInput);

  const previewVerdict = deriveMergeVerificationVerdict(buildConfigDraft);
  const effectiveVerdict = previewVerdict ?? detectedVerdict;
  const effectiveDefaultBranch = detectedDefaultBranch || previewDefaultBranch;

  const handleGitHubConnect = () => {
    void githubConnection.connect(() => {
      saveNewProjectOAuthResume({
        version: 1,
        repoInput,
        projectName,
        goal,
        projectType,
        users,
        team,
        deployCadence,
        notes,
        constraints,
        promotionTargetInput,
        useManualRepo,
        repoFilter,
        previewDefaultBranch,
      });
    });
  };

  const handleClose = () => {
    clearNewProjectOAuthResume();
    onClose();
  };

  const handleRepoNext = async () => {
    if (githubConnection.loading || !githubConnection.status?.identity_linked) {
      setRepoError('Connect your GitHub account before previewing this repository.');
      return;
    }
    if (!parsed) {
      setRepoError('Enter a valid GitHub repo as "owner/repo" or a full GitHub URL.');
      return;
    }
    setRepoError('');
    setPreviewError('');
    setBuildConfigDraft(null);
    setDetectedConfig(null);
    setDetectedVerdict(null);
    setPreviewLoading(true);
    setStep('config');
    try {
      const initialSteering = buildInitialSteeringPayload({
        projectType,
        users,
        team,
        deployCadence,
        notes,
        constraints,
      });
      const data = await previewOnboardProject(
        parsed.owner,
        parsed.repo,
        projectName.trim() || undefined,
        undefined,
        promotionTargetInput.trim() || undefined,
        goal.trim() || undefined,
        initialSteering,
      );
      setBuildConfigDraft(cloneBuildConfig(data.build_config));
      setDetectedConfig(cloneBuildConfig(data.build_config));
      setDetectedVerdict(data.merge_verification_verdict ?? null);
      setDetectedDefaultBranch(data.default_branch ?? previewDefaultBranch);
      setPreviewDefaultBranch(data.default_branch ?? previewDefaultBranch);
    } catch (e) {
      setBuildConfigDraft(null);
      setDetectedVerdict(null);
      setPreviewError(e instanceof Error ? e.message : 'Unable to preview build config');
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleCreate = async () => {
    if (!parsed || !buildConfigDraft) return;
    setStep('submitting');
    setSubmitError('');
    try {
      const effectivePromotionTarget = promotionTargetInput.trim() || undefined;
      const trimmedGoal = goal.trim();
      const initialSteering = buildInitialSteeringPayload({
        projectType,
        users,
        team,
        deployCadence,
        notes,
        constraints,
      });
      const data = await onboardProject(
        parsed.owner,
        parsed.repo,
        projectName.trim() || undefined,
        buildConfigDraft,
        effectivePromotionTarget,
        trimmedGoal || undefined,
        initialSteering,
      );
      const projectId = data.project_id;
      setCreatedProjectId(projectId);
      setDetectedConfig((data.build_config as BuildConfig) || null);
      setDetectedVerdict(data.merge_verification_verdict ?? null);
      setDetectedDefaultBranch((data as Record<string, unknown>).default_branch as string || '');
      setStep('success');
      clearNewProjectOAuthResume();
      onSuccess(projectId);

      // Load feature configs for the setup guide — optional, never undoes the success step
      setFeatureConfigLoading(true);
      try {
        const [arRes, iiRes] = await Promise.all([
          apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/auto-remediation`),
          apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/issue-implementation`),
        ]);
        if (arRes.ok) {
          const arData = await arRes.json();
          if (arData?.config) setAutoRemediationConfig(arData.config as AutoRemediationConfig);
        }
        if (iiRes.ok) {
          const iiData = await iiRes.json();
          if (iiData?.config) setIssueImplementationConfig(iiData.config as IssueImplementationConfig);
        }
      } catch {
        // feature-config is best-effort; project is already created
      } finally {
        setFeatureConfigLoading(false);
      }
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : 'Unknown error');
      setStep('config');
    }
  };

  const handleToggleAutoRemediation = async () => {
    if (!createdProjectId || !autoRemediationConfig) return;
    const prev = autoRemediationConfig;
    const next = !autoRemediationConfig.enabled;
    setAutoRemediationConfig({ ...autoRemediationConfig, enabled: next });
    setAutoRemToggleError('');
    setAutoRemSaving(true);
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(createdProjectId)}/auto-remediation`, {
        method: 'PATCH',
        body: JSON.stringify({ ...autoRemediationConfig, enabled: next }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data?.config) setAutoRemediationConfig(data.config as AutoRemediationConfig);
    } catch {
      setAutoRemediationConfig(prev);
      setAutoRemToggleError('Failed to update — change was not saved.');
    } finally {
      setAutoRemSaving(false);
    }
  };

  const handleToggleIssueImplementation = async () => {
    if (!createdProjectId || !issueImplementationConfig) return;
    const prev = issueImplementationConfig;
    const next = !issueImplementationConfig.enabled;
    setIssueImplementationConfig({ ...issueImplementationConfig, enabled: next });
    setIssueImplToggleError('');
    setIssueImplSaving(true);
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(createdProjectId)}/issue-implementation`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data?.config) setIssueImplementationConfig(data.config as IssueImplementationConfig);
    } catch {
      setIssueImplementationConfig(prev);
      setIssueImplToggleError('Failed to update — change was not saved.');
    } finally {
      setIssueImplSaving(false);
    }
  };

  const stepKeys = ['repo', 'config', 'success'] as const;
  const currentStepIndex = step === 'submitting' ? 1 : stepKeys.indexOf(step as typeof stepKeys[number]);

  const filteredRepos = repoOptions.filter(r =>
    r.full_name.toLowerCase().includes(repoFilter.toLowerCase())
  );

  const addConstraint = () => setConstraints((prev) => [...prev, '']);
  const updateConstraint = (idx: number, value: string) => {
    setConstraints((prev) => prev.map((constraint, i) => (i === idx ? value : constraint)));
  };
  const removeConstraint = (idx: number) => {
    setConstraints((prev) => (prev.length <= 1 ? [''] : prev.filter((_, i) => i !== idx)));
  };
  const updateBuildConfigDraft = (updater: (current: BuildConfig) => BuildConfig) => {
    setBuildConfigDraft((current) => (current ? updater(current) : current));
  };
  const updateDraftDirectory = (
    index: number,
    field: keyof BuildConfigDirectory,
    value: string,
  ) => {
    updateBuildConfigDraft((current) => ({
      ...current,
      directories: (current.directories ?? []).map((directory, dirIndex) => (
        dirIndex === index ? { ...directory, [field]: value } : directory
      )),
    }));
  };
  const addDraftDirectory = () => {
    updateBuildConfigDraft((current) => ({
      ...current,
      directories: [...(current.directories ?? []).map((directory) => ({ ...directory })), newBuildConfigDirectory()],
    }));
  };
  const removeDraftDirectory = (index: number) => {
    updateBuildConfigDraft((current) => ({
      ...current,
      directories: (current.directories ?? []).filter((_, dirIndex) => dirIndex !== index),
    }));
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div className="bg-neutral-900 rounded-xl shadow-2xl w-full max-w-[600px] mx-3 sm:mx-auto overflow-y-auto max-h-[90vh] border border-neutral-800">
        <div className="flex justify-between items-center p-5 border-b border-neutral-800">
          <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
            <Plus className="w-4 h-4 text-neutral-400" />
            {setupProject ? 'Project Setup' : 'New Project'}
          </h2>
          <button onClick={handleClose} className="min-h-[44px] min-w-[44px] flex items-center justify-center -mr-2 text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5">
          {/* Step indicators — hidden in setup-guide re-entry mode */}
          <div className={`flex items-center gap-2 mb-6 ${setupProject ? 'hidden' : ''}`}>
            {stepKeys.map((s, i) => {
              const isCompleted = i < currentStepIndex;
              const isCurrent = i === currentStepIndex;
              return (
                <div key={s} className="flex items-center gap-2">
                  <div className={`w-5 h-5 rounded-full flex items-center justify-center text-ui-sm font-bold transition-colors ${
                    isCompleted
                      ? 'bg-emerald-600 text-white'
                      : isCurrent
                      ? 'bg-white text-neutral-900'
                      : 'bg-neutral-800 text-neutral-600'
                  }`}>
                    {isCompleted ? <CheckCircle className="w-3 h-3" /> : i + 1}
                  </div>
                  <span className={`text-ui-md transition-colors ${isCurrent ? 'text-neutral-200' : `hidden sm:inline ${isCompleted ? 'text-neutral-400' : 'text-neutral-600'}`}`}>
                    {STEP_LABELS[s]}
                  </span>
                  {i < stepKeys.length - 1 && <span className="text-neutral-700 text-xs mx-1">›</span>}
                </div>
              );
            })}
          </div>

          {/* Step 1: Repository */}
          {step === 'repo' && (
            <div>
              <div className="mb-4">
                <div className="mb-4 rounded-lg border border-neutral-800 bg-neutral-950/70 p-3">
                  <div className="flex items-start gap-2.5">
                    <Github className="mt-0.5 h-4 w-4 shrink-0 text-neutral-400" />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-xs font-semibold text-neutral-200">Your GitHub authorization</p>
                        {githubConnection.loading ? (
                          <span className="text-ui-sm text-neutral-500">Checking…</span>
                        ) : githubConnection.status?.identity_linked ? (
                          <span className="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-1.5 py-0.5 text-ui-sm text-emerald-400">
                            Connected{githubConnection.status.account_name ? ` as ${githubConnection.status.account_name}` : ''}
                          </span>
                        ) : (
                          <span className="rounded-full border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 text-ui-sm text-amber-400">Not connected</span>
                        )}
                      </div>
                      <p className="mt-1 text-ui-sm leading-relaxed text-neutral-500">
                        Connect your account so Blueprint can verify that you personally have push or admin access. Separately, the Blueprint GitHub App must be installed with access to this repository so agents can work on it.
                      </p>
                      {!githubConnection.loading && !githubConnection.status?.identity_linked && (
                        <button
                          type="button"
                          onClick={handleGitHubConnect}
                          disabled={githubConnection.connecting}
                          className="mt-2 inline-flex min-h-[44px] items-center gap-1.5 rounded border border-neutral-700 bg-neutral-800 px-3 py-2 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {githubConnection.connecting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Github className="h-3.5 w-3.5" />}
                          Connect GitHub
                        </button>
                      )}
                      {githubConnection.error && (
                        <p className="mt-1 text-ui-sm text-amber-500">{githubConnection.error}</p>
                      )}
                      {!githubConnection.loading && githubConnection.status && !githubConnection.status.app_configured && (
                        <p className="mt-1 text-ui-sm text-amber-500">The Blueprint GitHub App is not configured on this server. Ask an administrator to configure it before onboarding repositories.</p>
                      )}
                      {githubConnection.status?.identity_linked && githubConnection.status.app_configured && !githubConnection.status.app_installation_reachable && (
                        <div className="mt-2">
                          <p className="text-ui-sm text-amber-500">Your GitHub identity is connected, but the Blueprint GitHub App has no repository installation you can access yet.</p>
                          <button
                            type="button"
                            onClick={handleGitHubConnect}
                            disabled={githubConnection.connecting}
                            className="mt-2 inline-flex min-h-[44px] items-center gap-1.5 rounded border border-neutral-700 bg-neutral-800 px-3 py-2 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {githubConnection.connecting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Github className="h-3.5 w-3.5" />}
                            Install Blueprint App
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
                <label className="block text-ui-md font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                  GitHub Repository
                </label>

                {repoLoading ? (
                  <div className="flex items-center gap-2 py-3 text-neutral-500 text-xs">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    Loading repositories…
                  </div>
                ) : !useManualRepo && repoOptions.length > 0 ? (
                  <div>
                    {/* Searchable repo list */}
                    <div className="relative mb-1">
                      <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-neutral-500 pointer-events-none" />
                      <input
                        type="text"
                        autoFocus
                        value={repoFilter}
                        onChange={e => setRepoFilter(e.target.value)}
                        placeholder="Filter repositories…"
                        className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg pl-8 pr-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                      />
                    </div>
                    <div className="max-h-48 overflow-y-auto rounded-lg border border-neutral-800 bg-neutral-950 mb-1">
                      {filteredRepos.length === 0 ? (
                        <div className="px-3 py-2 text-xs text-neutral-500">No repositories match</div>
                      ) : (
                        filteredRepos.map(r => (
                          <button
                            key={r.full_name}
                            type="button"
                            onClick={() => {
                              setRepoInput(r.full_name);
                              setRepoError('');
                              setPreviewDefaultBranch(r.default_branch);
                            }}
                            className={`w-full flex items-center justify-between px-3 py-2 min-h-[44px] text-left hover:bg-neutral-800 transition-colors border-b border-neutral-800/50 last:border-b-0 ${
                              repoInput === r.full_name ? 'bg-neutral-800' : ''
                            }`}
                          >
                            <span className="text-xs font-mono text-neutral-200">{r.full_name}</span>
                            <span className="text-ui-sm text-neutral-500 ml-2 shrink-0 flex items-center gap-1">
                              <GitBranch className="w-3 h-3" />
                              {r.default_branch}
                            </span>
                          </button>
                        ))
                      )}
                    </div>
                    {repoInput && parsed && !repoError && (
                      <p className="text-ui-sm text-emerald-500 mb-1 flex items-center gap-1">
                        <CheckCircle className="w-3 h-3" />
                        Selected: {parsed.owner}/{parsed.repo}
                      </p>
                    )}
                    <button
                      type="button"
                      onClick={() => { setUseManualRepo(true); setRepoInput(''); setRepoFilter(''); setPreviewDefaultBranch(''); }}
                      className="min-h-[44px] flex items-center text-ui-sm text-neutral-500 hover:text-neutral-300 underline transition-colors"
                    >
                      Enter manually
                    </button>
                  </div>
                ) : (
                  <div>
                    {repoFetchError && (
                      <p className="text-ui-sm text-amber-500 mb-1">{repoFetchError}</p>
                    )}
                    <input
                      type="text"
                      autoFocus
                      value={repoInput}
                      onChange={e => { setRepoInput(e.target.value); setRepoError(''); }}
                      onKeyDown={e => e.key === 'Enter' && void handleRepoNext()}
                      placeholder="owner/repo or https://github.com/owner/repo"
                      className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono ${
                        repoError ? 'border-red-500' : 'border-neutral-800'
                      }`}
                    />
                    {repoOptions.length > 0 && (
                      <button
                        type="button"
                        onClick={() => { setUseManualRepo(false); setRepoInput(''); }}
                        className="min-h-[44px] flex items-center text-ui-sm text-neutral-500 hover:text-neutral-300 underline transition-colors"
                      >
                        Pick from list
                      </button>
                    )}
                  </div>
                )}

                {repoError && <p className="text-ui-sm text-red-500 mt-1">{repoError}</p>}
                {!repoLoading && useManualRepo && parsed && !repoError && (
                  <p className="text-ui-sm text-emerald-500 mt-1 flex items-center gap-1">
                    <GitBranch className="w-3 h-3" />
                    {parsed.owner}/{parsed.repo}
                  </p>
                )}
              </div>

              <div className="mb-6">
                <label className="block text-ui-md font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                  Project Name{' '}
                  <span className="text-neutral-600 font-normal normal-case tracking-normal">(optional)</span>
                </label>
                <input
                  type="text"
                  value={projectName}
                  onChange={e => setProjectName(e.target.value)}
                  placeholder={parsed ? `${parsed.owner}/${parsed.repo}` : 'Defaults to owner/repo'}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                />
              </div>

              <div className="mb-4">
                <label className="block text-ui-md font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                  Goal{' '}
                  <span className="text-neutral-600 font-normal normal-case tracking-normal">(optional)</span>
                </label>
                <textarea
                  value={goal}
                  onChange={e => setGoal(e.target.value)}
                  rows={2}
                  placeholder="What do you want Blueprint to achieve on this repo?"
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 resize-none"
                />
                <p className="text-ui-xs text-neutral-600 mt-1">
                  Seeds the project's strategic intent so the first cycle reasons from your goal. Leave blank to skip.
                </p>
              </div>

              <div className="mb-6">
                <div className="mb-2">
                  <label className="block text-ui-md font-semibold text-neutral-400 uppercase tracking-wider">
                    Initial Steering{' '}
                    <span className="text-neutral-600 font-normal normal-case tracking-normal">(optional)</span>
                  </label>
                  <p className="text-ui-xs text-neutral-600 mt-1">
                    Capture project context and hard constraints so the first cycle starts with the right shape.
                  </p>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <label className="block">
                    <span className="block text-ui-sm text-neutral-500 mb-1">Project type</span>
                    <input
                      type="text"
                      value={projectType}
                      onChange={e => setProjectType(e.target.value)}
                      placeholder="startup / research / product / personal / enterprise"
                      className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    />
                  </label>
                  <label className="block">
                    <span className="block text-ui-sm text-neutral-500 mb-1">Team</span>
                    <input
                      type="text"
                      value={team}
                      onChange={e => setTeam(e.target.value)}
                      placeholder="solo / 3 engineers + 1 PM / etc."
                      className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    />
                  </label>
                  <label className="block">
                    <span className="block text-ui-sm text-neutral-500 mb-1">Users</span>
                    <input
                      type="text"
                      value={users}
                      onChange={e => setUsers(e.target.value)}
                      placeholder="Who this is for, in one line"
                      className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    />
                  </label>
                  <label className="block">
                    <span className="block text-ui-sm text-neutral-500 mb-1">Deploy cadence</span>
                    <input
                      type="text"
                      value={deployCadence}
                      onChange={e => setDeployCadence(e.target.value)}
                      placeholder="continuous / weekly / quarterly / sporadic"
                      className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    />
                  </label>
                </div>
                <label className="block mt-3">
                  <span className="block text-ui-sm text-neutral-500 mb-1">Notes</span>
                  <textarea
                    value={notes}
                    onChange={e => setNotes(e.target.value)}
                    rows={3}
                    placeholder="Risk tolerance, compliance constraints, or other context"
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 resize-none"
                  />
                </label>
                <div className="mt-4">
                  <div className="mb-2">
                    <span className="block text-ui-sm font-semibold text-neutral-400 uppercase tracking-wider">Constraints</span>
                    <p className="text-ui-xs text-neutral-600 mt-1">
                      Hard rules and preferences the agent must respect. One per line.
                    </p>
                  </div>
                  <div className="space-y-2">
                    {constraints.map((constraint, index) => (
                      <div key={index} className="flex items-start gap-2">
                        <input
                          type="text"
                          value={constraint}
                          onChange={e => updateConstraint(index, e.target.value)}
                          placeholder="e.g., Don't modify .github/workflows/"
                          className="flex-1 min-w-0 bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                        />
                        <button
                          type="button"
                          onClick={() => removeConstraint(index)}
                          className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center rounded border border-neutral-800 text-neutral-500 hover:text-rose-400 hover:bg-neutral-800/60 transition-colors"
                          aria-label="Remove constraint"
                        >
                          <X className="w-4 h-4" />
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      onClick={addConstraint}
                      className="min-h-[44px] inline-flex items-center gap-1 px-2 py-1 text-xs text-neutral-400 hover:text-neutral-100 transition-colors"
                    >
                      <Plus className="w-3 h-3" />
                      Add constraint
                    </button>
                  </div>
                </div>
              </div>

              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={handleClose}
                  className="px-4 py-1.5 min-h-[44px] sm:min-h-0 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => { void handleRepoNext(); }}
                  disabled={!repoInput.trim() || githubConnection.loading || !githubConnection.status?.identity_linked}
                  className="px-4 py-1.5 min-h-[44px] sm:min-h-0 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Continue
                </button>
              </div>
            </div>
          )}

          {/* Step 2: Confirm */}
          {(step === 'config' || step === 'submitting') && parsed && (
            <div>
              <div className="mb-4 p-3 bg-neutral-800/50 rounded-lg border border-neutral-700/50">
                <div className="flex items-center gap-2 mb-1">
                  <GitBranch className="w-3.5 h-3.5 text-neutral-400 shrink-0" />
                  <span className="text-xs font-mono text-neutral-200">{parsed.owner}/{parsed.repo}</span>
                </div>
                {projectName.trim() && (
                  <div className="text-ui-md text-neutral-500 mt-1">
                    Name: <span className="text-neutral-400">{projectName.trim()}</span>
                  </div>
                )}
              </div>

              {effectiveDefaultBranch && (
                <div className="mb-4">
                  <label className="block text-ui-md font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                    Branch Configuration
                  </label>
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-ui-sm">
                      <span className="text-neutral-500 w-28 shrink-0">Integration</span>
                      <span className="font-mono text-neutral-300">{effectiveDefaultBranch}</span>
                      <span className="text-neutral-600">(auto-detected)</span>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-ui-sm text-neutral-500 w-28 shrink-0 pt-2">Promotion target</span>
                      <div className="flex-1">
                        <input
                          type="text"
                          value={promotionTargetInput}
                          onChange={e => setPromotionTargetInput(e.target.value)}
                          placeholder={`${effectiveDefaultBranch} (same as integration)`}
                          className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-ui-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700"
                        />
                        <p className="text-ui-xs text-neutral-600 mt-1">
                          Leave blank to use the same as integration. Set a different branch for LTS workflows (e.g. <span className="font-mono">main</span> when integration is <span className="font-mono">automain</span>).
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {previewLoading ? (
                <div className="mb-4 flex items-center gap-2 py-3 text-neutral-500 text-xs">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  Detecting build config…
                </div>
              ) : previewError ? (
                <div className="mb-4 p-3 bg-amber-950/40 border border-amber-900/50 rounded-lg text-xs text-amber-300">
                  {installErrorBannerContent(previewError, parsed?.owner, parsed?.repo) ?? previewError}
                </div>
              ) : null}

              {buildConfigDraft && (
                <div className="mb-4 p-3 bg-neutral-950 rounded-lg border border-neutral-800">
                  <div className="flex items-center justify-between gap-2 mb-3">
                    <div>
                      <div className="text-ui-md font-semibold text-neutral-300 uppercase tracking-wider">
                        Merge-gate draft
                      </div>
                      <div className="text-ui-sm text-neutral-500 mt-1">
                        Edit the detected config before creating the project.
                      </div>
                    </div>
                    <label className="inline-flex items-center gap-2 text-ui-sm text-neutral-400">
                      <input
                        type="checkbox"
                        checked={buildConfigDraft.requires_ci_checks !== false}
                        onChange={e => updateBuildConfigDraft((current) => ({
                          ...current,
                          requires_ci_checks: e.target.checked,
                        }))}
                        className="h-4 w-4 rounded border-neutral-700 bg-neutral-900 text-white"
                      />
                      Require CI checks
                    </label>
                  </div>

                  <div className="space-y-3">
                    <div className="text-ui-sm text-neutral-500">
                      {buildConfigDraft.has_ci === false ? 'No CI detected. Turn off required CI checks or add a real test command.' : 'Detected build config can be adjusted here before creation.'}
                    </div>

                    {(buildConfigDraft.directories ?? []).length > 0 ? (
                      <div className="space-y-3">
                        {(buildConfigDraft.directories ?? []).map((directory, index) => (
                          <div key={index} className="rounded border border-neutral-800 bg-neutral-900/70 p-3">
                            <div className="mb-2 flex items-center justify-between gap-2">
                              <div className="text-ui-sm font-mono text-neutral-400">
                                Directory {index + 1}
                              </div>
                              <button
                                type="button"
                                onClick={() => removeDraftDirectory(index)}
                                className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center rounded border border-neutral-800 px-2 text-ui-sm text-neutral-500 hover:text-rose-400 hover:bg-neutral-800/60 transition-colors"
                                aria-label={`Remove directory ${index + 1}`}
                              >
                                <X className="w-3.5 h-3.5" />
                              </button>
                            </div>
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                              <label className="block">
                                <span className="block text-ui-sm text-neutral-500 mb-1">Path</span>
                                <input
                                  type="text"
                                  value={directory.path ?? ''}
                                  onChange={e => updateDraftDirectory(index, 'path', e.target.value)}
                                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-ui-md font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700"
                                />
                              </label>
                              <label className="block">
                                <span className="block text-ui-sm text-neutral-500 mb-1">Type</span>
                                <select
                                  value={directory.type ?? 'other'}
                                  onChange={e => updateDraftDirectory(index, 'type', e.target.value)}
                                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-ui-md focus:outline-none focus:ring-1 focus:ring-neutral-700"
                                >
                                  <option value="python">python</option>
                                  <option value="node">node</option>
                                  <option value="go">go</option>
                                  <option value="rust">rust</option>
                                  <option value="other">other</option>
                                </select>
                              </label>
                              <label className="block sm:col-span-2">
                                <span className="block text-ui-sm text-neutral-500 mb-1">Test command</span>
                                <input
                                  type="text"
                                  value={directory.test_cmd ?? ''}
                                  onChange={e => updateDraftDirectory(index, 'test_cmd', e.target.value)}
                                  placeholder="Leave blank to keep the merge gate unverified"
                                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-ui-md font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700"
                                />
                              </label>
                              <label className="block">
                                <span className="block text-ui-sm text-neutral-500 mb-1">Lint command</span>
                                <input
                                  type="text"
                                  value={directory.lint_cmd ?? ''}
                                  onChange={e => updateDraftDirectory(index, 'lint_cmd', e.target.value)}
                                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-ui-md font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700"
                                />
                              </label>
                              <label className="block">
                                <span className="block text-ui-sm text-neutral-500 mb-1">Build command</span>
                                <input
                                  type="text"
                                  value={directory.build_cmd ?? ''}
                                  onChange={e => updateDraftDirectory(index, 'build_cmd', e.target.value)}
                                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-ui-md font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700"
                                />
                              </label>
                              <label className="block sm:col-span-2">
                                <span className="block text-ui-sm text-neutral-500 mb-1">Install command</span>
                                <input
                                  type="text"
                                  value={directory.install_cmd ?? ''}
                                  onChange={e => updateDraftDirectory(index, 'install_cmd', e.target.value)}
                                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-ui-md font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700"
                                />
                              </label>
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="text-ui-sm text-neutral-500">
                        No directories detected yet. Add one below if you want the no-CI gate to run tests.
                      </div>
                    )}

                    <button
                      type="button"
                      onClick={addDraftDirectory}
                      className="min-h-[44px] inline-flex items-center gap-1 px-2 py-1 text-xs text-neutral-400 hover:text-neutral-100 transition-colors"
                    >
                      <Plus className="w-3 h-3" />
                      Add directory
                    </button>
                  </div>
                </div>
              )}

              {effectiveVerdict && (
                <div className="mb-4">
                  <MergeVerificationBanner verdict={effectiveVerdict} />
                </div>
              )}

              {submitError && (
                <div className="mb-4 p-3 bg-red-950/50 border border-red-900/50 rounded-lg text-xs text-red-400">
                  {installErrorBannerContent(submitError, parsed?.owner, parsed?.repo) ?? submitError}
                </div>
              )}

              <div className="flex justify-between gap-2">
                <button
                  type="button"
                  onClick={() => setStep('repo')}
                  disabled={step === 'submitting'}
                  className="px-4 py-1.5 min-h-[44px] sm:min-h-0 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                >
                  Back
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={handleClose}
                    disabled={step === 'submitting'}
                    className="px-4 py-1.5 min-h-[44px] sm:min-h-0 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleCreate}
                    disabled={step === 'submitting' || previewLoading || !buildConfigDraft}
                    className="px-4 py-1.5 min-h-[44px] sm:min-h-0 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors disabled:opacity-60 disabled:cursor-not-allowed flex items-center gap-1.5"
                  >
                    {step === 'submitting' && (
                      <Loader2 className="animate-spin" style={{ width: 14, height: 14 }} />
                    )}
                    {step === 'submitting' ? 'Creating...' : 'Create Project'}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Step 3: Success / Setup Guide */}
          {step === 'success' && (
            <div>
              {/* Header */}
              <div className="flex items-center gap-2 mb-5">
                <CheckCircle className="w-5 h-5 text-emerald-500 shrink-0" />
                <span className="text-sm font-semibold text-neutral-100">Project created</span>
              </div>

              {/* Repository confirmed */}
              <div className="mb-4 p-3 bg-neutral-800/50 rounded-lg border border-neutral-700/50">
                <div className="flex items-center gap-2">
                  <GitBranch className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
                  <span className="text-xs font-mono text-neutral-200">
                    {setupProject ? `${setupProject.owner}/${setupProject.repo}` : `${parsed?.owner}/${parsed?.repo}`}
                  </span>
                  {detectedDefaultBranch && (
                    <span className="ml-auto text-ui-sm text-neutral-500 flex items-center gap-1 shrink-0">
                      default: <span className="font-mono text-neutral-400">{detectedDefaultBranch}</span>
                    </span>
                  )}
                </div>
              </div>

              {/* Build config */}
              {detectedConfig && (
                <div className="mb-4">
                  <div className="flex items-center gap-1.5 mb-2">
                    <Wrench className="w-3 h-3 text-neutral-500" />
                    <span className="text-ui-md font-semibold text-neutral-400 uppercase tracking-wider">
                      Build Config Detected
                    </span>
                    {detectedConfig.language && (
                      <span className="ml-auto px-1.5 py-0.5 bg-blue-500/10 text-blue-400 rounded text-ui-sm font-mono border border-blue-500/20">
                        {detectedConfig.language}
                      </span>
                    )}
                  </div>
                  {detectedConfig.directories && detectedConfig.directories.length > 0 ? (
                    <div className="space-y-1.5">
                      {detectedConfig.directories.map((dir, i) => (
                        <div key={i} className="p-2 bg-neutral-950 rounded border border-neutral-800">
                          <div className="text-ui-md font-mono text-neutral-300 mb-1">{dir.path}</div>
                          {(['install_cmd', 'lint_cmd', 'build_cmd', 'test_cmd'] as const).map(cmd =>
                            dir[cmd] ? (
                              <div key={cmd} className="flex items-baseline gap-2 mb-0.5">
                                <span className="text-ui-sm text-neutral-600 w-14 shrink-0">
                                  {cmd.replace('_cmd', '')}:
                                </span>
                                <code className="text-ui-sm font-mono text-neutral-400">{dir[cmd]}</code>
                              </div>
                            ) : null
                          )}
                        </div>
                      ))}
                    </div>
                  ) : !detectedConfig.language ? (
                    <p className="text-xs text-neutral-500">
                      No build configuration detected. Configure it in project settings.
                    </p>
                  ) : null}

                  {/* CI-presence pre-flight (detection + guidance only). */}
                  {detectedConfig.has_ci === true && (
                    <div
                      data-testid="ci-status-banner"
                      className="mt-2 flex items-start gap-2 text-ui-md text-emerald-400 bg-emerald-950/30 border border-emerald-900/50 rounded-lg px-3 py-2"
                    >
                      <CheckCircle className="w-3.5 h-3.5 shrink-0 mt-px" />
                      <span>
                        CI detected (GitHub Actions workflows present) — merges will be gated on the
                        repository's CI checks.
                      </span>
                    </div>
                  )}
                  {detectedConfig.has_ci === false && (
                    <div
                      data-testid="ci-status-banner"
                      className="mt-2 flex items-start gap-2 text-ui-md text-amber-400 bg-amber-950/30 border border-amber-900/50 rounded-lg px-3 py-2"
                    >
                      <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
                      <span>
                        No CI detected. With "required CI checks" left on, PRs will stall waiting for
                        checks that never arrive. Turn it off in project settings so merges are gated on
                        this repo's detected tests instead.
                        {detectedConfig.directories?.some(d => d.test_cmd?.trim()) ? (
                          <span className="block mt-1 text-emerald-400">
                            A test command is configured — the no-CI gate will run tests as the merge verdict.
                          </span>
                        ) : (
                          <span className="block mt-1 text-amber-300">
                            No test command detected — PRs would merge UNVERIFIED. Add one in settings before
                            relying on the gate.
                          </span>
                        )}
                      </span>
                    </div>
                  )}
                </div>
              )}

              {/* Merge-gate verdict — tells the operator whether merges run tests or fall open to UNVERIFIED. */}
              {detectedVerdict && (
                <div className="mb-4">
                  <MergeVerificationBanner verdict={detectedVerdict} />
                </div>
              )}

              {/* Feature toggles */}
              <div className="mb-4">
                <div className="text-ui-md font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                  Automation Features
                </div>
                {(autoRemToggleError || issueImplToggleError) && (
                  <p className="text-ui-sm text-red-400 mb-2" data-testid="toggle-error">
                    {autoRemToggleError || issueImplToggleError}
                  </p>
                )}
                {featureConfigLoading ? (
                  <div className="flex items-center gap-2 py-3 text-neutral-500 text-xs">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    Loading feature config…
                  </div>
                ) : (
                  <div className="space-y-2">
                    {/* Auto-remediation toggle */}
                    <div className="p-3 bg-neutral-800/50 rounded-lg border border-neutral-700/50">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2 min-w-0">
                          <Zap className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                          <div className="min-w-0">
                            <div className="text-xs font-medium text-neutral-200">Auto-Remediation</div>
                            <div className="text-ui-sm text-neutral-500 mt-0.5">
                              Auto-fix CI failures on the default branch
                            </div>
                          </div>
                        </div>
                        <button
                          aria-label="Toggle Auto-Remediation"
                          onClick={handleToggleAutoRemediation}
                          disabled={autoRemSaving || !autoRemediationConfig}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ml-3 disabled:opacity-50 ${
                            autoRemediationConfig?.enabled ? 'bg-emerald-600' : 'bg-neutral-700'
                          }`}
                        >
                          <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
                            autoRemediationConfig?.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'
                          }`} />
                        </button>
                      </div>
                    </div>

                    {/* Issue-to-implementation toggle */}
                    <div className="p-3 bg-neutral-800/50 rounded-lg border border-neutral-700/50">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2 min-w-0">
                          <Tag className="w-3.5 h-3.5 text-purple-400 shrink-0" />
                          <div className="min-w-0">
                            <div className="text-xs font-medium text-neutral-200">Issue-to-Implementation</div>
                            <div className="text-ui-sm text-neutral-500 mt-0.5">
                              Label an issue{' '}
                              <code className="font-mono text-ui-xs bg-neutral-700 px-1 py-0.5 rounded">
                                {issueImplementationConfig?.trigger_label ?? 'auto:implement'}
                              </code>
                              {' '}to spawn a task
                            </div>
                          </div>
                        </div>
                        <button
                          aria-label="Toggle Issue-to-Implementation"
                          onClick={handleToggleIssueImplementation}
                          disabled={issueImplSaving || !issueImplementationConfig}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ml-3 disabled:opacity-50 ${
                            issueImplementationConfig?.enabled ? 'bg-emerald-600' : 'bg-neutral-700'
                          }`}
                        >
                          <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
                            issueImplementationConfig?.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'
                          }`} />
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Next steps */}
              <div className="mb-5 p-3 bg-blue-500/5 rounded-lg border border-blue-500/15">
                <div className="text-ui-md font-semibold text-blue-400 uppercase tracking-wider mb-2">
                  What happens next
                </div>
                <ul className="space-y-1.5 text-ui-md text-neutral-400">
                  <li className="flex items-start gap-2">
                    <ArrowRight className="w-3 h-3 text-neutral-600 mt-0.5 shrink-0" />
                    <span>Eligible PRs can be reviewed after you enable Auto-Review in project settings; webhook admission and project limits still apply</span>
                  </li>
                  {autoRemediationConfig?.enabled && detectedDefaultBranch && (
                    <li className="flex items-start gap-2">
                      <ArrowRight className="w-3 h-3 text-neutral-600 mt-0.5 shrink-0" />
                      <span>CI failures on <span className="font-mono text-neutral-300">{detectedDefaultBranch}</span> will trigger an automatic fix PR</span>
                    </li>
                  )}
                  {issueImplementationConfig?.enabled && (
                    <li className="flex items-start gap-2">
                      <ArrowRight className="w-3 h-3 text-neutral-600 mt-0.5 shrink-0" />
                      <span>Label a GitHub issue <code className="font-mono text-ui-xs bg-neutral-700 px-1 py-0.5 rounded">{issueImplementationConfig.trigger_label}</code> to automatically spawn an implementation task</span>
                    </li>
                  )}
                  <li className="flex items-start gap-2">
                    <ArrowRight className="w-3 h-3 text-neutral-600 mt-0.5 shrink-0" />
                    <span>Configure more options in project settings (build config, directions, advanced)</span>
                  </li>
                </ul>
              </div>

              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={handleClose}
                  className="px-4 py-1.5 min-h-[44px] sm:min-h-0 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors"
                >
                  Done
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
