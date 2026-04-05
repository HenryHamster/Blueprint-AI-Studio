import { useState, useRef } from 'react';
import { GitBranch, X, Plus, GitFork, Microscope, CheckCircle, AlertCircle, Loader2, ChevronDown, ArrowUpDown, Wrench } from 'lucide-react';

interface NewTaskModalProps {
  newTaskRepo: string;
  newTaskBranch: string;
  newTaskTitle: string;
  newTaskPrompt: string;
  newTaskType: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor';
  newTaskPrNumber: string;
  newTaskPrBranch: string;
  setNewTaskRepo: (value: string) => void;
  setNewTaskBranch: (value: string) => void;
  setNewTaskTitle: (value: string) => void;
  setNewTaskPrompt: (value: string) => void;
  setNewTaskType: (value: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor') => void;
  setNewTaskPrNumber: (value: string) => void;
  setNewTaskPrBranch: (value: string) => void;
  newTaskModel: string;
  newTaskEffort: string;
  setNewTaskModel: (value: string) => void;
  setNewTaskEffort: (value: string) => void;
  getDefaultModelForType: (taskType: string) => string;
  getDefaultEffortForType: (taskType: string) => string;
  repoOptions: Array<{ full_name: string; default_branch: string }>;
  repoLoading: boolean;
  repoError: string | null;
  useManualRepo: boolean;
  setUseManualRepo: React.Dispatch<React.SetStateAction<boolean>>;
  branchOptions: string[];
  branchLoading: boolean;
  branchError: string | null;
  useManualBranch: boolean;
  setUseManualBranch: React.Dispatch<React.SetStateAction<boolean>>;
  setBranchOptions: React.Dispatch<React.SetStateAction<string[]>>;
  onRepoChange: (value: string) => void;
  onSubmit: (e: React.FormEvent) => Promise<void>;
  onClose: () => void;
}

export function NewTaskModal({
  newTaskRepo,
  newTaskBranch,
  newTaskTitle,
  newTaskPrompt,
  newTaskType,
  newTaskPrNumber,
  newTaskPrBranch,
  setNewTaskRepo,
  setNewTaskBranch,
  setNewTaskTitle,
  setNewTaskPrompt,
  setNewTaskType,
  setNewTaskPrNumber,
  setNewTaskPrBranch,
  newTaskModel,
  newTaskEffort,
  setNewTaskModel,
  setNewTaskEffort,
  getDefaultModelForType,
  getDefaultEffortForType,
  repoOptions,
  repoLoading,
  repoError,
  useManualRepo,
  setUseManualRepo,
  branchOptions,
  branchLoading,
  branchError,
  useManualBranch,
  setUseManualBranch,
  setBranchOptions,
  onRepoChange,
  onSubmit,
  onClose,
}: NewTaskModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [prNumberError, setPrNumberError] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const prNumberRef = useRef<HTMLInputElement>(null);

  const handleTaskTypeChange = (value: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor') => {
    setNewTaskType(value);
    setNewTaskModel(getDefaultModelForType(value));
    setNewTaskEffort(getDefaultEffortForType(value));
  };

  const isPrNumberValid = (value: string) => /^[1-9]\d*$/.test(value);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitAttempted(true);
    if (repoError || branchError) return;
    if ((newTaskType === 'review' || newTaskType === 'fix') && !isPrNumberValid(newTaskPrNumber)) {
      setPrNumberError(true);
      prNumberRef.current?.focus();
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(e);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div className="bg-neutral-900 rounded-xl shadow-2xl w-full max-w-lg mx-3 sm:mx-auto overflow-y-auto max-h-[90vh] border border-neutral-800">
        <div className="flex justify-between items-center p-5 border-b border-neutral-800">
          <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
            <GitBranch className="w-4 h-4 text-neutral-400" />
            Launch Claude Code Task
          </h2>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-5">
          <div className="grid grid-cols-2 gap-4 mb-4">
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Repository</label>
                {!repoLoading && (
                  <button
                    type="button"
                    onClick={() => { setUseManualRepo(m => !m); if (!useManualRepo) { setNewTaskRepo(''); setBranchOptions([]); } }}
                    className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
                  >
                    {useManualRepo ? 'Use dropdown' : 'Type manually'}
                  </button>
                )}
              </div>
              {repoLoading ? (
                <div className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-500 font-mono">
                  Loading repos...
                </div>
              ) : useManualRepo ? (
                <>
                  <input
                    type="text"
                    required
                    value={newTaskRepo}
                    onChange={e => onRepoChange(e.target.value)}
                    className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono ${submitAttempted && repoError ? 'border-red-500' : 'border-neutral-800'}`}
                    placeholder="owner/repo"
                  />
                  {repoError && <p className="text-[10px] text-amber-500 mt-1">{repoError}</p>}
                </>
              ) : (
                <select
                  required
                  value={newTaskRepo}
                  onChange={e => onRepoChange(e.target.value)}
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer ${submitAttempted && repoError ? 'border-red-500' : 'border-neutral-800'}`}
                >
                  <option value="" disabled>Select a repository</option>
                  {repoOptions.map(r => (
                    <option key={r.full_name} value={r.full_name}>{r.full_name}</option>
                  ))}
                </select>
              )}
            </div>
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Base Branch</label>
                {!branchLoading && branchOptions.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setUseManualBranch(m => !m)}
                    className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
                  >
                    {useManualBranch ? 'Use dropdown' : 'Type manually'}
                  </button>
                )}
              </div>
              {branchLoading ? (
                <div className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-500 font-mono">
                  Loading branches...
                </div>
              ) : useManualBranch || branchOptions.length === 0 ? (
                <>
                  <input
                    type="text"
                    value={newTaskBranch}
                    onChange={e => setNewTaskBranch(e.target.value)}
                    className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono ${submitAttempted && branchError ? 'border-red-500' : 'border-neutral-800'}`}
                    placeholder="main"
                  />
                  {branchError && <p className="text-[10px] text-amber-500 mt-1">{branchError}</p>}
                </>
              ) : (
                <select
                  value={newTaskBranch}
                  onChange={e => setNewTaskBranch(e.target.value)}
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer ${submitAttempted && branchError ? 'border-red-500' : 'border-neutral-800'}`}
                >
                  {branchOptions.map(b => (
                    <option key={b} value={b}>{b}</option>
                  ))}
                </select>
              )}
            </div>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Task Type</label>
            <div className="flex gap-2">
              {([
                { value: 'create' as const, label: 'Create', desc: 'Write code, open PR', icon: Plus },
                { value: 'refactor' as const, label: 'Refactor', desc: 'Improve code quality', icon: Wrench },
                { value: 'plan' as const, label: 'Plan', desc: 'Decompose into subtasks', icon: GitFork },
                { value: 'research' as const, label: 'Research', desc: 'Analyze codebase', icon: Microscope },
                { value: 'review' as const, label: 'Review', desc: 'Review existing PR', icon: CheckCircle },
                { value: 'fix' as const, label: 'Fix', desc: 'Fix PR from feedback', icon: AlertCircle },
                { value: 'migrate' as const, label: 'Migrate', desc: 'Generate DB migration', icon: ArrowUpDown },
              ]).map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => handleTaskTypeChange(opt.value)}
                  className={`flex-1 flex items-center gap-2 px-3 py-2 rounded-lg border text-xs font-medium transition-all ${
                    newTaskType === opt.value
                      ? 'bg-white/5 border-neutral-600 text-neutral-100'
                      : 'bg-neutral-950 border-neutral-800 text-neutral-500 hover:border-neutral-700'
                  }`}
                >
                  <opt.icon className="w-3.5 h-3.5" />
                  <div className="text-left">
                    <div>{opt.label}</div>
                    <div className="text-[10px] text-neutral-500 font-normal">{opt.desc}</div>
                  </div>
                </button>
              ))}
            </div>
          </div>
          <div className={`transition-all duration-150 ${(newTaskType === 'review' || newTaskType === 'fix') ? 'opacity-100 h-auto mb-4' : 'opacity-0 h-0 overflow-hidden'}`}>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">PR Number</label>
                <input
                  ref={prNumberRef}
                  type="number"
                  min="1"
                  value={newTaskPrNumber}
                  onChange={e => { setNewTaskPrNumber(e.target.value); if (prNumberError) setPrNumberError(false); }}
                  onBlur={() => setPrNumberError(!isPrNumberValid(newTaskPrNumber))}
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 ${prNumberError ? 'border-red-500' : 'border-neutral-800'}`}
                  placeholder="e.g., 15"
                />
                {prNumberError && <p className="text-[10px] text-red-500 mt-1">PR number must be a positive integer.</p>}
              </div>
              <div>
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">PR Branch</label>
                <input
                  type="text"
                  value={newTaskPrBranch}
                  onChange={e => setNewTaskPrBranch(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                  placeholder="e.g., blueprint/task-abc123"
                />
              </div>
            </div>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Task Title</label>
            <input
              type="text"
              required
              autoFocus
              value={newTaskTitle}
              onChange={e => setNewTaskTitle(e.target.value)}
              className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
              placeholder="e.g., Add user authentication"
            />
          </div>
          <div className="mb-6">
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Prompt / Instructions</label>
            <div className="relative">
              <textarea
                required
                value={newTaskPrompt}
                onChange={e => setNewTaskPrompt(e.target.value)}
                className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 resize-y min-h-[160px] max-h-[400px] transition-all placeholder:text-neutral-600"
                placeholder="Describe what Claude Code should do in this repository. Be specific about files, patterns, and expected outcomes..."
              />
              <span className="text-[10px] text-neutral-600 absolute bottom-2 right-2 pointer-events-none">{newTaskPrompt.length} chars</span>
            </div>
          </div>
          <div className="mb-4">
            <button
              type="button"
              onClick={() => setAdvancedOpen(o => !o)}
              className="flex items-center gap-1 text-[11px] font-semibold text-neutral-500 hover:text-neutral-300 uppercase tracking-wider transition-colors"
            >
              <ChevronDown className={`w-3 h-3 transition-transform ${advancedOpen ? 'rotate-180' : ''}`} />
              Advanced
            </button>
            {advancedOpen && (
              <div className="grid grid-cols-2 gap-3 mt-3">
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Model</label>
                  <select
                    value={newTaskModel}
                    onChange={e => setNewTaskModel(e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
                  >
                    <option value="">Default</option>
                    <option value="claude-opus-4-6">claude-opus-4-6</option>
                    <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                    <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Effort</label>
                  <select
                    value={newTaskEffort}
                    onChange={e => setNewTaskEffort(e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
                  >
                    <option value="">Default</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="max">max</option>
                  </select>
                </div>
              </div>
            )}
          </div>
          <div className="flex justify-end gap-2 items-center">
            {submitAttempted && (repoError || branchError) && (
              <span className="text-[11px] text-red-400 mr-auto">Fix errors above before launching.</span>
            )}
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-1.5 min-h-[44px] sm:min-h-0 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-1.5 min-h-[44px] sm:min-h-0 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors disabled:opacity-60 disabled:cursor-not-allowed flex items-center gap-1.5"
            >
              {submitting && <Loader2 className="animate-spin" style={{ width: 14, height: 14 }} />}
              Launch
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
