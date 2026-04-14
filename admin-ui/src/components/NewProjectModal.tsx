import { useState } from 'react';
import { X, GitBranch, Loader2, CheckCircle, Plus } from 'lucide-react';
import { onboardProject } from '../api';
import type { BuildConfig } from '../types/project';

type Step = 'repo' | 'config' | 'submitting' | 'success';

interface NewProjectModalProps {
  onClose: () => void;
  onSuccess: (projectId: string) => void;
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

export function NewProjectModal({ onClose, onSuccess }: NewProjectModalProps) {
  const [step, setStep] = useState<Step>('repo');
  const [repoInput, setRepoInput] = useState('');
  const [projectName, setProjectName] = useState('');
  const [repoError, setRepoError] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [createdProjectId, setCreatedProjectId] = useState('');
  const [detectedConfig, setDetectedConfig] = useState<BuildConfig | null>(null);

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
      const data = await onboardProject(
        parsed.owner,
        parsed.repo,
        projectName.trim() || undefined,
      );
      setCreatedProjectId(data.project_id);
      setDetectedConfig((data.build_config as BuildConfig) || null);
      setStep('success');
      onSuccess(data.project_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : 'Unknown error');
      setStep('config');
    }
  };

  const stepKeys = ['repo', 'config', 'success'] as const;
  const currentStepIndex = step === 'submitting' ? 1 : stepKeys.indexOf(step as typeof stepKeys[number]);

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div className="bg-neutral-900 rounded-xl shadow-2xl w-full max-w-[600px] mx-3 sm:mx-auto overflow-y-auto max-h-[90vh] border border-neutral-800">
        <div className="flex justify-between items-center p-5 border-b border-neutral-800">
          <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
            <Plus className="w-4 h-4 text-neutral-400" />
            New Project
          </h2>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5">
          {/* Step indicators */}
          <div className="flex items-center gap-2 mb-6">
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
                  <span className={`text-[11px] transition-colors ${isCurrent ? 'text-neutral-200' : isCompleted ? 'text-neutral-400' : 'text-neutral-600'}`}>
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
                <input
                  type="text"
                  autoFocus
                  value={repoInput}
                  onChange={e => { setRepoInput(e.target.value); setRepoError(''); }}
                  onKeyDown={e => e.key === 'Enter' && handleRepoNext()}
                  placeholder="owner/repo or https://github.com/owner/repo"
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono ${
                    repoError ? 'border-red-500' : 'border-neutral-800'
                  }`}
                />
                {repoError && <p className="text-[10px] text-red-500 mt-1">{repoError}</p>}
                {parsed && !repoError && (
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
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                />
              </div>

              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={onClose}
                  className="px-4 py-1.5 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleRepoNext}
                  disabled={!repoInput.trim()}
                  className="px-4 py-1.5 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
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
                  className="px-4 py-1.5 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                >
                  Back
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={onClose}
                    disabled={step === 'submitting'}
                    className="px-4 py-1.5 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleCreate}
                    disabled={step === 'submitting'}
                    className="px-4 py-1.5 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors disabled:opacity-60 disabled:cursor-not-allowed flex items-center gap-1.5"
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

          {/* Step 3: Success */}
          {step === 'success' && (
            <div>
              <div className="flex items-center gap-2 mb-4">
                <CheckCircle className="w-5 h-5 text-emerald-500 shrink-0" />
                <span className="text-sm font-semibold text-neutral-100">Project created</span>
              </div>

              <div className="mb-4 p-3 bg-neutral-800/50 rounded-lg border border-neutral-700/50">
                <div className="text-[11px] text-neutral-500 mb-1">Project ID</div>
                <div className="text-xs font-mono text-neutral-300 break-all">{createdProjectId}</div>
              </div>

              {detectedConfig && (
                <div className="mb-5">
                  <div className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">
                    Detected Build Config
                  </div>

                  {detectedConfig.language && (
                    <div className="flex items-center gap-2 mb-3">
                      <span className="text-[11px] text-neutral-500">Language:</span>
                      <span className="px-2 py-0.5 bg-blue-500/10 text-blue-400 rounded text-[11px] font-mono border border-blue-500/20">
                        {detectedConfig.language}
                      </span>
                    </div>
                  )}

                  {detectedConfig.directories && detectedConfig.directories.length > 0 ? (
                    <div className="space-y-2">
                      {detectedConfig.directories.map((dir, i) => (
                        <div key={i} className="p-2.5 bg-neutral-950 rounded border border-neutral-800">
                          <div className="text-[11px] font-mono text-neutral-300 mb-2">{dir.path}</div>
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
                  ) : (
                    !detectedConfig.language && (
                      <p className="text-xs text-neutral-500">
                        No build configuration detected. Configure it in project settings.
                      </p>
                    )
                  )}
                </div>
              )}

              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={onClose}
                  className="px-4 py-1.5 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors"
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
