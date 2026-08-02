/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect, useRef } from 'react';
import { Plus, Trash2, RefreshCw, AlertTriangle, CheckCircle } from 'lucide-react';

function isRecognizedLintCmd(cmd: string): boolean {
  if (!cmd.trim()) return true;
  const base = cmd.replace(/^(python3?\s+-m\s+|npx\s+|pnpm\s+exec\s+)/, '');
  return /^(ruff|eslint|mypy|flake8|pyright)\b/.test(base);
}
import type { BuildConfig, BuildConfigDirectory, MergeGateRuntime, MergeVerificationVerdict } from '../types/project';
import { apiFetch, getBlockDegradedUnverifiedMerge, setBlockDegradedUnverifiedMerge, getBlockNoTestUnverifiedMerge, setBlockNoTestUnverifiedMerge } from '../api';
import { SR_CANONICAL_CATEGORIES } from './SrRetrospectiveShared';
import { MergeVerificationBanner } from './MergeVerificationBanner';

interface ProjectBuildConfigSectionProps {
  githubOwner: string | null;
  githubRepo: string | null;
  isSelfRepo?: boolean;
  buildConfig: BuildConfig | null;
  onSave: (config: BuildConfig) => Promise<void>;
  onDetect: () => Promise<void>;
  detectAvailable: boolean;
  projectId?: string | null;
  /**
   * Backend-computed merge-gate verdict. When provided, it is rendered directly;
   * when omitted, the component derives it from the project list it already fetches
   * (the same single source as the backend list endpoint), so the settings panel
   * needs no extra app-state wiring. Pass `null` to explicitly suppress.
   */
  mergeVerificationVerdict?: MergeVerificationVerdict | null;
  /** Optional live no-CI gate runtime evidence. When omitted, the component derives it from the project list. */
  mergeGateRuntime?: MergeGateRuntime | null;
}

const EMPTY_DIR: BuildConfigDirectory = {
  path: '',
  type: '',
  lint_cmd: '',
  test_cmd: '',
  build_cmd: '',
};

function newDir(): BuildConfigDirectory {
  return { ...EMPTY_DIR };
}

export function ProjectBuildConfigSection({
  githubOwner,
  githubRepo,
  buildConfig,
  onSave,
  onDetect,
  detectAvailable,
  projectId,
  mergeVerificationVerdict,
  mergeGateRuntime,
}: ProjectBuildConfigSectionProps) {
  const hasConfig = buildConfig && (
    (buildConfig.directories?.length ?? 0) > 0 ||
    buildConfig.git_user_email ||
    buildConfig.git_user_name
  );

  const [dirs, setDirs] = useState<BuildConfigDirectory[]>(() =>
    buildConfig?.directories?.length ? buildConfig.directories : []
  );
  const [gitEmail, setGitEmail] = useState(
    buildConfig?.git_user_email ?? 'blueprint-agent@automated.dev'
  );
  const [gitName, setGitName] = useState(
    buildConfig?.git_user_name ?? 'Blueprint Agent'
  );
  const [enableParallelReview, setEnableParallelReview] = useState(
    buildConfig?.enable_parallel_review ?? false
  );
  const [enableParallelFix, setEnableParallelFix] = useState(
    buildConfig?.enable_parallel_fix ?? false
  );
  const [requiresCiChecks, setRequiresCiChecks] = useState(
    buildConfig?.requires_ci_checks ?? true
  );
  const [taskBranchNamespace, setTaskBranchNamespace] = useState(
    buildConfig?.task_branch_namespace ?? ''
  );
  const [taskBranchPrefix, setTaskBranchPrefix] = useState(
    buildConfig?.task_branch_prefix ?? ''
  );
  const [prTitlePrefix, setPrTitlePrefix] = useState(
    buildConfig?.pr_title_prefix ?? ''
  );
  const [stagingPrefixes, setStagingPrefixes] = useState<string[]>([]);
  const [stagingPrefixesIsDefault, setStagingPrefixesIsDefault] = useState(true);
  const [stagingPrefixesSaving, setStagingPrefixesSaving] = useState(false);
  const [stagingPrefixesError, setStagingPrefixesError] = useState('');
  const [blockDegradedEnabled, setBlockDegradedEnabled] = useState(false);
  const [blockDegradedSaving, setBlockDegradedSaving] = useState(false);
  const [blockNoTestEnabled, setBlockNoTestEnabled] = useState(false);
  const [blockNoTestSaving, setBlockNoTestSaving] = useState(false);
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [srTargets, setSrTargets] = useState<Record<string, number>>({});
  // Verdict derived from the project list when the parent doesn't pass one
  // explicitly. The prop, when supplied (incl. null), takes precedence.
  const [fetchedVerdict, setFetchedVerdict] = useState<MergeVerificationVerdict | null>(null);
  const [fetchedRuntime, setFetchedRuntime] = useState<MergeGateRuntime | null>(null);
  const effectiveVerdict = mergeVerificationVerdict !== undefined ? mergeVerificationVerdict : fetchedVerdict;
  const effectiveRuntime = mergeGateRuntime !== undefined ? mergeGateRuntime : fetchedRuntime;
  const preservedBranchOnDetect = useRef<{ namespace: string; prefix: string; prTitlePrefix: string } | null>(null);

  // Sync state when buildConfig prop changes (e.g. after detect or project switch)
  useEffect(() => {
    setDirs(buildConfig?.directories?.length ? buildConfig.directories : []);
    setGitEmail(buildConfig?.git_user_email ?? 'blueprint-agent@automated.dev');
    setGitName(buildConfig?.git_user_name ?? 'Blueprint Agent');
    setEnableParallelReview(buildConfig?.enable_parallel_review ?? false);
    setEnableParallelFix(buildConfig?.enable_parallel_fix ?? false);
    setRequiresCiChecks(buildConfig?.requires_ci_checks ?? true);
    const preserved = preservedBranchOnDetect.current;
    setTaskBranchNamespace(buildConfig?.task_branch_namespace ?? preserved?.namespace ?? '');
    setTaskBranchPrefix(buildConfig?.task_branch_prefix ?? preserved?.prefix ?? '');
    setPrTitlePrefix(buildConfig?.pr_title_prefix ?? preserved?.prTitlePrefix ?? '');
    preservedBranchOnDetect.current = null;
  }, [buildConfig]);

  // Load honest-block policy flag — only relevant when CI checks are off
  useEffect(() => {
    if (!projectId || requiresCiChecks) { setBlockDegradedEnabled(false); return; }
    getBlockDegradedUnverifiedMerge(projectId)
      .then(resp => setBlockDegradedEnabled(resp.block_degraded_unverified_merge))
      .catch(() => {});
  }, [projectId, requiresCiChecks]);

  // Load no-test-command hold policy flag — only relevant when CI checks are off
  useEffect(() => {
    if (!projectId || requiresCiChecks) { setBlockNoTestEnabled(false); return; }
    getBlockNoTestUnverifiedMerge(projectId)
      .then(resp => setBlockNoTestEnabled(resp.block_no_test_unverified_merge))
      .catch(() => {});
  }, [projectId, requiresCiChecks]);

  // Load staging-branch-prefixes from the dedicated endpoint
  useEffect(() => {
    if (!projectId) { setStagingPrefixes([]); setStagingPrefixesIsDefault(true); return; }
    apiFetch(`/api/admin/projects/${projectId}/staging-branch-prefixes`)
      .then(r => r.json())
      .then((resp: { staging_branch_prefixes: string[]; is_default: boolean }) => {
        setStagingPrefixes(resp.staging_branch_prefixes);
        setStagingPrefixesIsDefault(resp.is_default);
      })
      .catch(() => {});
  }, [projectId]);

  // Load SR catch-rate targets from the project when projectId changes
  useEffect(() => {
    if (!projectId) { setSrTargets({}); return; }
    apiFetch('/api/admin/projects')
      .then(r => r.json())
      .then((resp: { items: { id: string; sr_catch_rate_targets?: Record<string, number> }[] }) => {
        const p = (resp.items ?? []).find(p => p.id === projectId);
        setSrTargets(p?.sr_catch_rate_targets ?? {});
      })
      .catch(() => {});
  }, [projectId]);

  // Derive the merge-verification verdict from the project list (the backend's
  // single source of truth) unless the parent supplies it as a prop. Re-runs when
  // buildConfig changes (after detect/save) so the banner reflects persisted state.
  // apiFetch dedups this concurrent GET with the SR-targets fetch above.
  useEffect(() => {
    if (!projectId) { setFetchedVerdict(null); setFetchedRuntime(null); return; }
    let cancelled = false;
    apiFetch('/api/admin/projects')
      .then(r => r.json())
      .then((resp: { items?: { id: string; merge_verification_verdict?: MergeVerificationVerdict; merge_gate_runtime?: MergeGateRuntime }[] }) => {
        if (cancelled) return;
        const p = (resp.items ?? []).find(p => p.id === projectId);
        setFetchedRuntime(p?.merge_gate_runtime ?? null);
        if (mergeVerificationVerdict === undefined) {
          setFetchedVerdict(p?.merge_verification_verdict ?? null);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [projectId, buildConfig, mergeVerificationVerdict]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave({
        ...buildConfig,
        directories: dirs,
        git_user_email: gitEmail,
        git_user_name: gitName,
        enable_parallel_review: enableParallelReview,
        enable_parallel_fix: enableParallelFix,
        requires_ci_checks: requiresCiChecks,
        task_branch_namespace: taskBranchNamespace.trim() || undefined,
        task_branch_prefix: taskBranchPrefix.trim() || undefined,
        pr_title_prefix: prTitlePrefix.trim() || undefined,
      });
      if (projectId) {
        await apiFetch(`/api/admin/projects/${projectId}`, {
          method: 'PATCH',
          body: JSON.stringify({ sr_catch_rate_targets: srTargets }),
        });
      }
    } finally {
      setSaving(false);
    }
  };

  const handleSaveStagingPrefixes = async () => {
    if (!projectId) return;
    const trimmed = stagingPrefixes.map(p => p.trim()).filter(Boolean);
    setStagingPrefixesSaving(true);
    setStagingPrefixesError('');
    try {
      const resp = await apiFetch(`/api/admin/projects/${projectId}/staging-branch-prefixes`, {
        method: 'PATCH',
        body: JSON.stringify({ staging_branch_prefixes: trimmed }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setStagingPrefixes(data.staging_branch_prefixes);
      setStagingPrefixesIsDefault(data.is_default);
    } catch {
      setStagingPrefixesError('Failed to save staging prefixes — changes were not applied.');
    } finally {
      setStagingPrefixesSaving(false);
    }
  };

  const handleRevertStagingPrefixes = async () => {
    if (!projectId) return;
    setStagingPrefixesSaving(true);
    setStagingPrefixesError('');
    try {
      const resp = await apiFetch(`/api/admin/projects/${projectId}/staging-branch-prefixes`, {
        method: 'PATCH',
        body: JSON.stringify({ staging_branch_prefixes: null }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setStagingPrefixes(data.staging_branch_prefixes);
      setStagingPrefixesIsDefault(data.is_default);
    } catch {
      setStagingPrefixesError('Failed to revert staging prefixes — changes were not applied.');
    } finally {
      setStagingPrefixesSaving(false);
    }
  };

  const handleDetect = async () => {
    preservedBranchOnDetect.current = { namespace: taskBranchNamespace, prefix: taskBranchPrefix, prTitlePrefix };
    setDetecting(true);
    try {
      await onDetect();
    } finally {
      setDetecting(false);
    }
  };

  const updateDir = (index: number, field: keyof BuildConfigDirectory, value: string) => {
    setDirs(prev => prev.map((d, i) => i === index ? { ...d, [field]: value } : d));
  };

  const removeDir = (index: number) => {
    setDirs(prev => prev.filter((_, i) => i !== index));
  };

  const addDir = () => {
    setDirs(prev => [...prev, newDir()]);
  };

  return (
    <div className="space-y-6">
      {/* Repository info */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Repository</h3>
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 flex items-center gap-2">
          <span className="text-xs text-neutral-400">Repository</span>
          <span className="text-xs text-neutral-100 font-mono ml-auto">
            {githubOwner && githubRepo ? `${githubOwner}/${githubRepo}` : '—'}
          </span>
        </div>
        {buildConfig?.language ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            <span className="text-ui-md bg-neutral-800 border border-neutral-700 text-neutral-300 px-2 py-0.5 rounded-full">
              {buildConfig.language}
            </span>
          </div>
        ) : null}
      </div>

      {/* Empty state / detect prompt */}
      {!hasConfig && (
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-5 py-6 text-center space-y-3">
          <p className="text-xs text-neutral-400">
            No build configuration detected yet. Run auto-detection to populate lint, test, and build commands from the repository structure.
          </p>
          {detectAvailable ? (
            <button
              onClick={handleDetect}
              disabled={detecting}
              className="inline-flex items-center gap-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-200 rounded-lg px-4 py-2 text-xs transition-colors disabled:opacity-50"
            >
              {detecting ? (
                <RefreshCw className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RefreshCw className="w-3.5 h-3.5" />
              )}
              Detect Build Config
            </button>
          ) : (
            <button
              onClick={addDir}
              className="inline-flex items-center gap-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-200 rounded-lg px-4 py-2 text-xs transition-colors"
            >
              <Plus className="w-3.5 h-3.5" />
              Add manually
            </button>
          )}
        </div>
      )}

      {/* Directories table */}
      {hasConfig && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider">Directories</h3>
            <div className="flex items-center gap-2">
              {detectAvailable && (
                // reason: inline icon-only re-detect button; w-3.5 h-3.5 icon in compact config header
                <button
                  data-viewport-allow-small-target
                  onClick={handleDetect}
                  disabled={detecting}
                  title="Re-detect from repository"
                  className="text-neutral-500 hover:text-neutral-300 transition-colors disabled:opacity-50"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${detecting ? 'animate-spin' : ''}`} />
                </button>
              )}
            </div>
          </div>
          <p className="text-ui-md text-neutral-600 mb-3">
            Configure lint, test, and build commands per directory. The agent runs these in CI validation. Leave <code className="font-mono text-neutral-400">lint</code> empty to auto-infer from directory type and scope to changed files.
          </p>
          <div className="space-y-3">
            {dirs.map((dir, i) => (
              <div key={i} className="bg-neutral-900 border border-neutral-800 rounded-lg p-3 space-y-2">
                <div className="flex items-center gap-2 mb-1">
                  <div className="flex-1">
                    <label className="block text-ui-sm text-neutral-500 mb-0.5 uppercase">Path</label>
                    <input
                      type="text"
                      value={dir.path}
                      onChange={e => updateDir(i, 'path', e.target.value)}
                      placeholder="backend/"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                  <div className="flex-1">
                    <label className="block text-ui-sm text-neutral-500 mb-0.5 uppercase">Type</label>
                    <input
                      type="text"
                      value={dir.type}
                      onChange={e => updateDir(i, 'type', e.target.value)}
                      placeholder="python"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                  {/* reason: inline icon-only remove button in dense directory row */}
                  <button
                    data-viewport-allow-small-target
                    onClick={() => removeDir(i)}
                    className="mt-4 text-neutral-600 hover:text-red-400 transition-colors shrink-0"
                    title="Remove directory"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <label className="block text-ui-sm text-neutral-500 mb-0.5 uppercase">Lint</label>
                    <input
                      type="text"
                      value={dir.lint_cmd ?? ''}
                      onChange={e => updateDir(i, 'lint_cmd', e.target.value)}
                      placeholder="(auto-infer by type)"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                    {!isRecognizedLintCmd(dir.lint_cmd ?? '') && (
                      <p className="mt-1 flex items-start gap-1 text-ui-sm text-yellow-400" data-testid="lint-cmd-warning">
                        <AlertTriangle className="w-3 h-3 shrink-0 mt-px" />
                        Unrecognized — runs against full tree. Use ruff, eslint, mypy, flake8, or pyright for scoped lint.
                      </p>
                    )}
                  </div>
                  <div>
                    <label className="block text-ui-sm text-neutral-500 mb-0.5 uppercase">Test</label>
                    <input
                      type="text"
                      value={dir.test_cmd ?? ''}
                      onChange={e => updateDir(i, 'test_cmd', e.target.value)}
                      placeholder="pytest"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                  <div>
                    <label className="block text-ui-sm text-neutral-500 mb-0.5 uppercase">Build</label>
                    <input
                      type="text"
                      value={dir.build_cmd ?? ''}
                      onChange={e => updateDir(i, 'build_cmd', e.target.value)}
                      placeholder="npm run build"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>
          {/* reason: inline text link to add directory; text-xs single-line button in config section */}
          <button
            data-viewport-allow-small-target
            onClick={addDir}
            className="mt-3 flex items-center gap-1.5 text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            Add directory
          </button>
        </div>
      )}

      {/* Git identity */}
      {hasConfig && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Git Identity</h3>
          <p className="text-ui-md text-neutral-600 mb-3">
            Used for commits authored by the Blueprint agent.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-neutral-400 mb-1">Commit Email</label>
              <input
                type="email"
                value={gitEmail}
                onChange={e => setGitEmail(e.target.value)}
                placeholder="blueprint-agent@automated.dev"
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs text-neutral-400 mb-1">Commit Name</label>
              <input
                type="text"
                value={gitName}
                onChange={e => setGitName(e.target.value)}
                placeholder="Blueprint Agent"
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
          </div>
        </div>
      )}

      {/* Agent Behavior */}
      {hasConfig && (
        <div className="space-y-4">
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Agent Behavior</h3>
          <label className="flex items-start gap-3 cursor-pointer group">
            <div className="mt-0.5 shrink-0">
              <input
                type="checkbox"
                checked={enableParallelReview}
                onChange={e => setEnableParallelReview(e.target.checked)}
                className="sr-only peer"
                id="parallel-review-toggle"
              />
              <div
                onClick={() => setEnableParallelReview(v => !v)}
                className={`w-8 h-4 rounded-full transition-colors cursor-pointer ${enableParallelReview ? 'bg-blue-600' : 'bg-neutral-700'} relative`}
              >
                <div className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${enableParallelReview ? 'translate-x-4' : 'translate-x-0'}`} />
              </div>
            </div>
            <div>
              <span className="text-xs text-neutral-200 font-medium">Parallel Review Fan-Out</span>
              <p className="text-ui-md text-neutral-500 mt-0.5">
                On rollup PRs touching multiple modules, the reviewer fans out per-module investigation subagents
                and then does its own cross-module coherence pass. Reduces wall-clock on big reviews; auto-skipped
                for trivial diffs.
              </p>
            </div>
          </label>
          <label className="flex items-start gap-3 cursor-pointer group">
            <div className="mt-0.5 shrink-0">
              <input
                type="checkbox"
                checked={enableParallelFix}
                onChange={e => setEnableParallelFix(e.target.checked)}
                className="sr-only peer"
                id="parallel-fix-toggle"
              />
              <div
                onClick={() => setEnableParallelFix(v => !v)}
                className={`w-8 h-4 rounded-full transition-colors cursor-pointer ${enableParallelFix ? 'bg-blue-600' : 'bg-neutral-700'} relative`}
              >
                <div className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${enableParallelFix ? 'translate-x-4' : 'translate-x-0'}`} />
              </div>
            </div>
            <div>
              <span className="text-xs text-neutral-200 font-medium">Parallel Fix Investigation</span>
              <p className="text-ui-md text-neutral-500 mt-0.5">
                On fix iterations with multi-module diffs or many reviewer issues, the fix agent fans out
                root-cause investigation subagents (investigation only — parent applies all edits).
                Reduces wall-clock on large rollup fixes; auto-skipped for trivial diffs.
              </p>
            </div>
          </label>
          <label className="flex items-start gap-3 cursor-pointer group">
            <div className="mt-0.5 shrink-0">
              <input
                type="checkbox"
                checked={requiresCiChecks}
                onChange={e => setRequiresCiChecks(e.target.checked)}
                className="sr-only peer"
                id="requires-ci-checks-toggle"
              />
              <div
                onClick={() => setRequiresCiChecks(v => !v)}
                className={`w-8 h-4 rounded-full transition-colors cursor-pointer ${requiresCiChecks ? 'bg-blue-600' : 'bg-neutral-700'} relative`}
              >
                <div className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${requiresCiChecks ? 'translate-x-4' : 'translate-x-0'}`} />
              </div>
            </div>
            <div>
              <span className="text-xs text-neutral-200 font-medium">Repository has required CI checks</span>
              <p className="text-ui-md text-neutral-500 mt-0.5">
                On by default — Blueprint waits for the repository's required CI checks to pass before merging a PR.
                Turn this off for repos without required checks: Blueprint then runs the repository's detected test suite
                as the merge gate—passing tests merge, failing tests block. If no test command is detected, Blueprint proceeds unverified.
              </p>
            </div>
          </label>
          {!requiresCiChecks && projectId && (
            <label className="flex items-start gap-3 cursor-pointer group">
              <div className="mt-0.5 shrink-0">
                <input
                  type="checkbox"
                  checked={blockDegradedEnabled}
                  onChange={() => {}}
                  className="sr-only peer"
                  id="block-degraded-toggle"
                />
                <div
                  onClick={() => {
                    if (blockDegradedSaving) return;
                    const next = !blockDegradedEnabled;
                    setBlockDegradedEnabled(next);
                    setBlockDegradedSaving(true);
                    setBlockDegradedUnverifiedMerge(projectId, next)
                      .then(resp => setBlockDegradedEnabled(resp.block_degraded_unverified_merge))
                      .catch(err => { console.error('Failed to update block-degraded-unverified-merge:', err); setBlockDegradedEnabled(!next); })
                      .finally(() => setBlockDegradedSaving(false));
                  }}
                  className={`w-8 h-4 rounded-full transition-colors cursor-pointer ${blockDegradedEnabled ? 'bg-blue-600' : 'bg-neutral-700'} relative`}
                >
                  <div className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${blockDegradedEnabled ? 'translate-x-4' : 'translate-x-0'}`} />
                </div>
              </div>
              <div>
                <span className="text-xs text-neutral-200 font-medium">
                  Block degraded unverified merges
                  {blockDegradedSaving && <RefreshCw className="inline ml-1.5 w-3 h-3 animate-spin text-neutral-400" />}
                </span>
                <p className="text-ui-md text-neutral-500 mt-0.5">
                  When the test suite can't run (setup failure, suite skipped, probe error), Blueprint normally falls open and merges anyway.
                  With this enabled, Blueprint holds the PR instead — requiring operator action to unblock.
                </p>
              </div>
            </label>
          )}
          {!requiresCiChecks && projectId && (
            <label className="flex items-start gap-3 cursor-pointer group">
              <div className="mt-0.5 shrink-0">
                <input
                  type="checkbox"
                  checked={blockNoTestEnabled}
                  onChange={() => {}}
                  className="sr-only peer"
                  id="block-no-test-toggle"
                />
                <div
                  onClick={() => {
                    if (blockNoTestSaving) return;
                    const next = !blockNoTestEnabled;
                    setBlockNoTestEnabled(next);
                    setBlockNoTestSaving(true);
                    setBlockNoTestUnverifiedMerge(projectId, next)
                      .then(resp => setBlockNoTestEnabled(resp.block_no_test_unverified_merge))
                      .catch(err => { console.error('Failed to update block-no-test-unverified-merge:', err); setBlockNoTestEnabled(!next); })
                      .finally(() => setBlockNoTestSaving(false));
                  }}
                  className={`w-8 h-4 rounded-full transition-colors cursor-pointer ${blockNoTestEnabled ? 'bg-blue-600' : 'bg-neutral-700'} relative`}
                >
                  <div className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${blockNoTestEnabled ? 'translate-x-4' : 'translate-x-0'}`} />
                </div>
              </div>
              <div>
                <span className="text-xs text-neutral-200 font-medium">
                  Hold no-test-command merges for review
                  {blockNoTestSaving && <RefreshCw className="inline ml-1.5 w-3 h-3 animate-spin text-neutral-400" />}
                </span>
                <p className="text-ui-md text-neutral-500 mt-0.5">
                  When no test command is detected, Blueprint normally merges PRs UNVERIFIED.
                  With this enabled, Blueprint holds these PRs for operator action instead.
                </p>
              </div>
            </label>
          )}
          {/* Backend-computed verdict for what the merge gate will actually do. */}
          <MergeVerificationBanner verdict={effectiveVerdict} runtime={effectiveRuntime} blockDegradedEnabled={!requiresCiChecks && blockDegradedEnabled} blockNoTestEnabled={!requiresCiChecks && blockNoTestEnabled} />

          {/* CI-presence pre-flight: detection + guidance only (never auto-flips the toggle). */}
          {buildConfig?.has_ci === true && (
            <div
              data-testid="ci-status-banner"
              className="ml-11 -mt-2 flex items-start gap-2 text-ui-md text-emerald-400 bg-emerald-950/30 border border-emerald-900/50 rounded-lg px-3 py-2"
            >
              <CheckCircle className="w-3.5 h-3.5 shrink-0 mt-px" />
              <span>
                CI detected (GitHub Actions workflows present) — with "required CI checks" on, merges
                will be gated on the repository's CI.
              </span>
            </div>
          )}
          {buildConfig?.has_ci === false && (
            <div
              data-testid="ci-status-banner"
              className="ml-11 -mt-2 flex items-start gap-2 text-ui-md text-amber-400 bg-amber-950/30 border border-amber-900/50 rounded-lg px-3 py-2"
            >
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
              <span>
                No CI detected (no GitHub Actions workflows). Leaving "required CI checks" on will make
                every PR poll for checks that never arrive until the merge backstop times out. Turn it
                off so Blueprint gates merges on this repo's own detected test suite instead.
                {dirs.some(d => d.test_cmd?.trim()) ? (
                  <span className="block mt-1 text-emerald-400">
                    A test command is configured, so the no-CI gate will run tests as the merge verdict.
                  </span>
                ) : (
                  <span className="block mt-1 text-amber-300">
                    No test command is configured — with CI checks off, PRs would merge UNVERIFIED. Add a
                    test command below before relying on the gate.
                  </span>
                )}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Task Branch Naming */}
      {hasConfig && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Task Branch Naming</h3>
          <p className="text-ui-md text-neutral-600 mb-3">
            Controls the leading segment of agent task branches. Default namespace is <code className="font-mono text-neutral-400">blueprint</code>.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-neutral-400 mb-1">Namespace</label>
              <input
                type="text"
                value={taskBranchNamespace}
                onChange={e => setTaskBranchNamespace(e.target.value)}
                placeholder="blueprint"
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
              <p className="text-ui-md text-neutral-600 mt-1">
                Leading segment of agent task branches, e.g. <code className="font-mono text-neutral-400">{(taskBranchNamespace.trim() || 'blueprint')}/task-…</code>.
              </p>
            </div>
            <div>
              <label className="block text-xs text-neutral-400 mb-1">Prefix</label>
              <input
                type="text"
                value={taskBranchPrefix}
                onChange={e => setTaskBranchPrefix(e.target.value)}
                placeholder="blueprint/"
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
              <p className="text-ui-md text-neutral-600 mt-1">
                Legacy override. Leave blank to derive from namespace.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Staging Branch Prefixes */}
      {hasConfig && projectId && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Staging Branch Prefixes</h3>
          <p className="text-ui-md text-neutral-600 mb-3">
            Branch prefixes Blueprint treats as staging branches. Gates rollup and auto-merge routing. Defaults to <code className="font-mono text-neutral-400">research/</code> and <code className="font-mono text-neutral-400">plan/</code>.
          </p>
          <div className="flex items-center gap-2 mb-3">
            {stagingPrefixesIsDefault ? (
              <span className="text-ui-sm uppercase tracking-wide px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-500 border border-neutral-700">global default</span>
            ) : (
              <span className="text-ui-sm uppercase tracking-wide px-1.5 py-0.5 rounded bg-cyan-950/40 text-cyan-400 border border-cyan-900/50">override</span>
            )}
          </div>
          <div className="space-y-2 mb-3">
            {stagingPrefixes.map((prefix, i) => (
              <div key={i} className="flex items-center gap-2">
                <input
                  type="text"
                  value={prefix}
                  onChange={e => setStagingPrefixes(prev => prev.map((p, j) => j === i ? e.target.value : p))}
                  placeholder="research/"
                  className="flex-1 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                />
                {/* reason: inline icon-only remove button in dense prefix list */}
                <button
                  data-viewport-allow-small-target
                  onClick={() => setStagingPrefixes(prev => prev.filter((_, j) => j !== i))}
                  className="text-neutral-600 hover:text-red-400 transition-colors shrink-0"
                  title="Remove prefix"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
          </div>
          {/* reason: inline text link to add prefix; text-xs single-line button in config section */}
          <button
            data-viewport-allow-small-target
            onClick={() => setStagingPrefixes(prev => [...prev, ''])}
            className="flex items-center gap-1.5 text-xs text-neutral-500 hover:text-neutral-300 transition-colors mb-4"
          >
            <Plus className="w-3.5 h-3.5" />
            Add prefix
          </button>
          <div className="flex items-center gap-3 flex-wrap">
            {!stagingPrefixesIsDefault && (
              <button
                onClick={handleRevertStagingPrefixes}
                disabled={stagingPrefixesSaving}
                className="inline-flex items-center justify-center min-h-[44px] bg-neutral-800 hover:bg-neutral-700 text-neutral-400 rounded-lg px-4 py-2 text-xs transition-colors disabled:opacity-50"
              >
                {stagingPrefixesSaving ? 'Reverting...' : 'Revert to default'}
              </button>
            )}
            <button
              onClick={handleSaveStagingPrefixes}
              disabled={stagingPrefixesSaving || stagingPrefixes.length === 0 || stagingPrefixes.some(p => !p.trim())}
              className="inline-flex items-center justify-center min-h-[44px] bg-neutral-700 hover:bg-neutral-600 text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50 ml-auto"
            >
              {stagingPrefixesSaving ? 'Saving...' : 'Save Staging Prefixes'}
            </button>
          </div>
          {stagingPrefixesError && (
            <p className="text-ui-sm text-red-400 mt-2" data-testid="staging-prefixes-error">{stagingPrefixesError}</p>
          )}
        </div>
      )}

      {/* PR Title Prefix */}
      {hasConfig && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Pull Request</h3>
          <p className="text-ui-md text-neutral-600 mb-3">
            Optional prefix prepended to agent-authored PR titles. Leave blank to use no prefix.
          </p>
          <div>
            <label className="block text-xs text-neutral-400 mb-1">PR Title Prefix</label>
            <input
              type="text"
              value={prTitlePrefix}
              onChange={e => setPrTitlePrefix(e.target.value)}
              placeholder="[Bot]"
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <p className="text-ui-md text-neutral-600 mt-1">
              e.g. <code className="font-mono text-neutral-400">{prTitlePrefix.trim() || '[Bot]'}</code>{' '}feat: add new feature
            </p>
          </div>
        </div>
      )}

      {/* SR Catch-Rate Targets */}
      {hasConfig && projectId && (
        <div id="sr-catch-rate-targets">
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-1">Self-Review Catch-Rate Targets</h3>
          <p className="text-ui-md text-neutral-600 mb-3">
            Set a minimum hit-rate (%) per SR category. An alert fires when a category stays below target for ≥2 consecutive windows. Leave blank for no target.
          </p>
          <div className="grid grid-cols-3 gap-2">
            {SR_CANONICAL_CATEGORIES.map(({ id, label }) => (
              <div key={id}>
                <label className="block text-ui-sm text-neutral-500 mb-0.5 uppercase">{label}</label>
                <div className="flex items-center gap-1">
                  <input
                    type="number"
                    min="0"
                    max="100"
                    step="5"
                    value={srTargets[id] !== undefined ? Math.round(srTargets[id] * 100) : ''}
                    onChange={e => {
                      const raw = e.target.value;
                      setSrTargets(prev => {
                        if (raw === '') {
                          return Object.fromEntries(Object.entries(prev).filter(([k]) => k !== id));
                        }
                        return { ...prev, [id]: Math.min(100, Math.max(0, Number(raw))) / 100 };
                      });
                    }}
                    placeholder="—"
                    className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-base md:text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                  />
                  <span className="text-ui-sm text-neutral-500 shrink-0">%</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Save button */}
      {hasConfig && (
        <div className="flex justify-end pt-2">
          <button
            onClick={handleSave}
            disabled={saving}
            className="inline-flex items-center justify-center min-h-[44px] bg-neutral-700 hover:bg-neutral-600 text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Save Build Config'}
          </button>
        </div>
      )}
    </div>
  );
}
