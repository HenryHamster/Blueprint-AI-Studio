import { useState, useEffect } from 'react';
import { X, GitBranch, Loader2, CheckCircle, Plus, Search, ArrowRight, Wrench, Zap, Tag } from 'lucide-react';
import { onboardProject, authGet, apiFetch, API_BASE_URL } from '../api';
import type { BuildConfig, AutoRemediationConfig } from '../types/project';

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
}

function parseRepoInput(input: string): { owner: string; repo: string } | null {
  const trimmed = input.trim();
  const urlMatch = trimmed.match(/github\.com\/([^/]+)\/([^/\s]+?)(?:\.git)?(?:\/.*)?$/);
  if (urlMatch) return { owner: urlMatch[1], repo: urlMatch[2] };
  const shortMatch = trimmed.match(/^([a-zA-Z0-9_.-]+)\/([a-zA-Z0-9_.-]+)$/);
  if (shortMatch) return { owner: shortMatch[1], repo: shortMatch[2] };
  return null;
}

const STEP_LABELS: Record<string, string> = { repo: 'Repository', config: 'Confirm', success: 'Created' };

export function NewProjectModal({ onClose, onSuccess, setupProject }: NewProjectModalProps) {
  const [step, setStep] = useState<Step>(setupProject ? 'success' : 'repo');
  const [repoInput, setRepoInput] = useState('');
  const [projectName, setProjectName] = useState('');
  const [repoError, setRepoError] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [createdProjectId, setCreatedProjectId] = useState(setupProject?.id ?? '');
  const [detectedConfig, setDetectedConfig] = useState<BuildConfig | null>(null);

  // Repo picker state
  const [detectedDefaultBranch, setDetectedDefaultBranch] = useState('');
  const [previewDefaultBranch, setPreviewDefaultBranch] = useState('');
  const [promotionTargetInput, setPromotionTargetInput] = useState('');

  // Feature configs for the post-install setup guide
  const [autoRemediationConfig, setAutoRemediationConfig] = useState<AutoRemediationConfig | null>(null);
  const [issueImplementationConfig, setIssueImplementationConfig] = useState<IssueImplementationConfig | null>(null);
  const [featureConfigLoading, setFeatureConfigLoading] = useState(false);
  const [autoRemSaving, setAutoRemSaving] = useState(false);
  const [issueImplSaving, setIssueImplSaving] = useState(false);

  // Repo picker state
  const [repoOptions, setRepoOptions] = useState<RepoOption[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoFetchError, setRepoFetchError] = useState<string | null>(null);
  const [useManualRepo, setUseManualRepo] = useState(false);
  const [repoFilter, setRepoFilter] = useState('');

  useEffect(() => {
    let cancelled = false;
    setRepoLoading(true);
    setRepoFetchError(null);
    authGet(`${API_BASE_URL}/api/admin/github/repos`)
      .then(async res => {
        if (cancelled) return;
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          setRepoFetchError(errData?.detail || 'Failed to load repositories');
          setUseManualRepo(true);
          return;
        }
        const data = await res.json();
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
  }, []);

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

  const handleRepoNext = () => {
    if (!parsed) {
      setRepoError('Enter a valid GitHub repo as "owner/repo" or a full GitHub URL.');
      return;
    }
    setRepoError('');
    setStep('config');
  };

  const handleCreate = async () => {
    if (!parsed) return;
    setStep('submitting');
    setSubmitError('');
    try {
      const effectivePromotionTarget = promotionTargetInput.trim() || undefined;
      const data = await onboardProject(
        parsed.owner,
        parsed.repo,
        projectName.trim() || undefined,
        undefined,
        effectivePromotionTarget,
      );
      const projectId = data.project_id;
      setCreatedProjectId(projectId);
      setDetectedConfig((data.build_config as BuildConfig) || null);
      setDetectedDefaultBranch((data as Record<string, unknown>).default_branch as string || '');
      setStep('success');
      onSuccess(projectId);

      // Load feature configs for the setup guide
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
    const next = !autoRemediationConfig.enabled;
    setAutoRemediationConfig({ ...autoRemediationConfig, enabled: next });
    setAutoRemSaving(true);
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(createdProjectId)}/auto-remediation`, {
        method: 'PATCH',
        body: JSON.stringify({ ...autoRemediationConfig, enabled: next }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data?.config) setAutoRemediationConfig(data.config as AutoRemediationConfig);
      }
    } catch {
      setAutoRemediationConfig({ ...autoRemediationConfig, enabled: !next });
    } finally {
      setAutoRemSaving(false);
    }
  };

  const handleToggleIssueImplementation = async () => {
    if (!createdProjectId || !issueImplementationConfig) return;
    const next = !issueImplementationConfig.enabled;
    setIssueImplementationConfig({ ...issueImplementationConfig, enabled: next });
    setIssueImplSaving(true);
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(createdProjectId)}/issue-implementation`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: next }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data?.config) setIssueImplementationConfig(data.config as IssueImplementationConfig);
      }
    } catch {
      setIssueImplementationConfig({ ...issueImplementationConfig, enabled: !next });
    } finally {
      setIssueImplSaving(false);
    }
  };

  const stepKeys = ['repo', 'config', 'success'] as const;
  const currentStepIndex = step === 'submitting' ? 1 : stepKeys.indexOf(step as typeof stepKeys[number]);

  const filteredRepos = repoOptions.filter(r =>
    r.full_name.toLowerCase().includes(repoFilter.toLowerCase())
  );

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div className="bg-neutral-900 rounded-xl shadow-2xl w-full max-w-[600px] mx-3 sm:mx-auto overflow-y-auto max-h-[90vh] border border-neutral-800">
        <div className="flex justify-between items-center p-5 border-b border-neutral-800">
          <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
            <Plus className="w-4 h-4 text-neutral-400" />
            {setupProject ? 'Project Setup' : 'New Project'}
          </h2>
          <button onClick={onClose} className="min-h-[44px] min-w-[44px] flex items-center justify-center -mr-2 text-neutral-500 hover:text-neutral-300 transition-colors">
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
                  <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold transition-colors ${
                    isCompleted
                      ? 'bg-emerald-600 text-white'
                      : isCurrent
                      ? 'bg-white text-neutral-900'
                      : 'bg-neutral-800 text-neutral-600'
                  }`}>
                    {isCompleted ? <CheckCircle className="w-3 h-3" /> : i + 1}
                  </div>
                  <span className={`text-[11px] transition-colors ${isCurrent ? 'text-neutral-200' : `hidden sm:inline ${isCompleted ? 'text-neutral-400' : 'text-neutral-600'}`}`}>
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
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">
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
                            <span className="text-[10px] text-neutral-500 ml-2 shrink-0 flex items-center gap-1">
                              <GitBranch className="w-3 h-3" />
                              {r.default_branch}
                            </span>
                          </button>
                        ))
                      )}
                    </div>
                    {repoInput && parsed && !repoError && (
                      <p className="text-[10px] text-emerald-500 mb-1 flex items-center gap-1">
                        <CheckCircle className="w-3 h-3" />
                        Selected: {parsed.owner}/{parsed.repo}
                      </p>
                    )}
                    <button
                      type="button"
                      onClick={() => { setUseManualRepo(true); setRepoInput(''); setRepoFilter(''); setPreviewDefaultBranch(''); }}
                      className="min-h-[44px] flex items-center text-[10px] text-neutral-500 hover:text-neutral-300 underline transition-colors"
                    >
                      Enter manually
                    </button>
                  </div>
                ) : (
                  <div>
                    {repoFetchError && !useManualRepo && (
                      <p className="text-[10px] text-amber-500 mb-1">{repoFetchError}</p>
                    )}
                    <input
                      type="text"
                      autoFocus
                      value={repoInput}
                      onChange={e => { setRepoInput(e.target.value); setRepoError(''); }}
                      onKeyDown={e => e.key === 'Enter' && handleRepoNext()}
                      placeholder="owner/repo or https://github.com/owner/repo"
                      className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono ${
                        repoError ? 'border-red-500' : 'border-neutral-800'
                      }`}
                    />
                    {repoOptions.length > 0 && (
                      <button
                        type="button"
                        onClick={() => { setUseManualRepo(false); setRepoInput(''); }}
                        className="min-h-[44px] flex items-center text-[10px] text-neutral-500 hover:text-neutral-300 underline transition-colors"
                      >
                        Pick from list
                      </button>
                    )}
                  </div>
                )}

                {repoError && <p className="text-[10px] text-red-500 mt-1">{repoError}</p>}
                {!repoLoading && useManualRepo && parsed && !repoError && (
                  <p className="text-[10px] text-emerald-500 mt-1 flex items-center gap-1">
                    <GitBranch className="w-3 h-3" />
                    {parsed.owner}/{parsed.repo}
                  </p>
                )}
              </div>

              <div className="mb-6">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">
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

              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={onClose}
                  className="px-4 py-1.5 min-h-[44px] sm:min-h-0 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleRepoNext}
                  disabled={!repoInput.trim()}
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
                  <div className="text-[11px] text-neutral-500 mt-1">
                    Name: <span className="text-neutral-400">{projectName.trim()}</span>
                  </div>
                )}
              </div>

              {previewDefaultBranch && (
                <div className="mb-4">
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                    Branch Configuration
                  </label>
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-[10px]">
                      <span className="text-neutral-500 w-28 shrink-0">Integration</span>
                      <span className="font-mono text-neutral-300">{previewDefaultBranch}</span>
                      <span className="text-neutral-600">(auto-detected)</span>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-[10px] text-neutral-500 w-28 shrink-0 pt-2">Promotion target</span>
                      <div className="flex-1">
                        <input
                          type="text"
                          value={promotionTargetInput}
                          onChange={e => setPromotionTargetInput(e.target.value)}
                          placeholder={`${previewDefaultBranch} (same as integration)`}
                          className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded px-2 py-1.5 text-[10px] font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700"
                        />
                        <p className="text-[9px] text-neutral-600 mt-1">
                          Leave blank to use the same as integration. Set a different branch for LTS workflows (e.g. <span className="font-mono">main</span> when integration is <span className="font-mono">automain</span>).
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              <p className="text-xs text-neutral-400 mb-4 leading-relaxed">
                Build configuration will be auto-detected from the repository's root files
                (package.json, pyproject.toml, go.mod, Cargo.toml, etc.). You can adjust it
                after creation in project settings.
              </p>

              {submitError && (
                <div className="mb-4 p-3 bg-red-950/50 border border-red-900/50 rounded-lg text-xs text-red-400">
                  {submitError}
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
                    onClick={onClose}
                    disabled={step === 'submitting'}
                    className="px-4 py-1.5 min-h-[44px] sm:min-h-0 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleCreate}
                    disabled={step === 'submitting'}
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
                    <span className="ml-auto text-[10px] text-neutral-500 flex items-center gap-1 shrink-0">
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
                    <span className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">
                      Build Config Detected
                    </span>
                    {detectedConfig.language && (
                      <span className="ml-auto px-1.5 py-0.5 bg-blue-500/10 text-blue-400 rounded text-[10px] font-mono border border-blue-500/20">
                        {detectedConfig.language}
                      </span>
                    )}
                  </div>
                  {detectedConfig.directories && detectedConfig.directories.length > 0 ? (
                    <div className="space-y-1.5">
                      {detectedConfig.directories.map((dir, i) => (
                        <div key={i} className="p-2 bg-neutral-950 rounded border border-neutral-800">
                          <div className="text-[11px] font-mono text-neutral-300 mb-1">{dir.path}</div>
                          {(['install_cmd', 'lint_cmd', 'build_cmd', 'test_cmd'] as const).map(cmd =>
                            dir[cmd] ? (
                              <div key={cmd} className="flex items-baseline gap-2 mb-0.5">
                                <span className="text-[10px] text-neutral-600 w-14 shrink-0">
                                  {cmd.replace('_cmd', '')}:
                                </span>
                                <code className="text-[10px] font-mono text-neutral-400">{dir[cmd]}</code>
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
                </div>
              )}

              {/* Feature toggles */}
              <div className="mb-4">
                <div className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                  Automation Features
                </div>
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
                            <div className="text-[10px] text-neutral-500 mt-0.5">
                              Auto-fix CI failures on the default branch
                            </div>
                          </div>
                        </div>
                        <button
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
                            <div className="text-[10px] text-neutral-500 mt-0.5">
                              Label an issue{' '}
                              <code className="font-mono text-[9px] bg-neutral-700 px-1 py-0.5 rounded">
                                {issueImplementationConfig?.trigger_label ?? 'auto:implement'}
                              </code>
                              {' '}to spawn a task
                            </div>
                          </div>
                        </div>
                        <button
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
                <div className="text-[11px] font-semibold text-blue-400 uppercase tracking-wider mb-2">
                  What happens next
                </div>
                <ul className="space-y-1.5 text-[11px] text-neutral-400">
                  <li className="flex items-start gap-2">
                    <ArrowRight className="w-3 h-3 text-neutral-600 mt-0.5 shrink-0" />
                    <span>Open a PR and Blueprint will automatically review it in the Checks tab</span>
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
                      <span>Label a GitHub issue <code className="font-mono text-[9px] bg-neutral-700 px-1 py-0.5 rounded">{issueImplementationConfig.trigger_label}</code> to automatically spawn an implementation task</span>
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
                  onClick={onClose}
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
