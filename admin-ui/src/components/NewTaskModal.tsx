import { GitBranch, X, Plus, GitFork, Microscope, CheckCircle, AlertCircle } from 'lucide-react';

interface NewTaskModalProps {
  newTaskRepo: string;
  newTaskBranch: string;
  newTaskTitle: string;
  newTaskPrompt: string;
  newTaskType: 'create' | 'plan' | 'research' | 'review' | 'fix';
  newTaskPrNumber: string;
  newTaskPrBranch: string;
  setNewTaskRepo: (value: string) => void;
  setNewTaskBranch: (value: string) => void;
  setNewTaskTitle: (value: string) => void;
  setNewTaskPrompt: (value: string) => void;
  setNewTaskType: (value: 'create' | 'plan' | 'research' | 'review' | 'fix') => void;
  setNewTaskPrNumber: (value: string) => void;
  setNewTaskPrBranch: (value: string) => void;
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
  onSubmit: (e: React.FormEvent) => void;
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
  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div className="bg-neutral-900 rounded-xl shadow-2xl w-full max-w-lg overflow-hidden border border-neutral-800">
        <div className="flex justify-between items-center p-5 border-b border-neutral-800">
          <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
            <GitBranch className="w-4 h-4 text-neutral-400" />
            Launch Claude Code Task
          </h2>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>
        <form onSubmit={onSubmit} className="p-5">
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
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono"
                    placeholder="owner/repo"
                  />
                  {repoError && <p className="text-[10px] text-amber-500 mt-1">{repoError}</p>}
                </>
              ) : (
                <select
                  required
                  value={newTaskRepo}
                  onChange={e => onRepoChange(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer"
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
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono"
                    placeholder="main"
                  />
                  {branchError && <p className="text-[10px] text-amber-500 mt-1">{branchError}</p>}
                </>
              ) : (
                <select
                  value={newTaskBranch}
                  onChange={e => setNewTaskBranch(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer"
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
                { value: 'plan' as const, label: 'Plan', desc: 'Decompose into subtasks', icon: GitFork },
                { value: 'research' as const, label: 'Research', desc: 'Analyze codebase', icon: Microscope },
                { value: 'review' as const, label: 'Review', desc: 'Review existing PR', icon: CheckCircle },
                { value: 'fix' as const, label: 'Fix', desc: 'Fix PR from feedback', icon: AlertCircle },
              ]).map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setNewTaskType(opt.value)}
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
                  type="number"
                  value={newTaskPrNumber}
                  onChange={e => setNewTaskPrNumber(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                  placeholder="e.g., 15"
                />
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
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-1.5 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="px-4 py-1.5 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors"
            >
              Launch
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
