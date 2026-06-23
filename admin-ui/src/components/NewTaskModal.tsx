import { useState, useRef } from 'react';
import { GitBranch, X, Plus, GitFork, Microscope, CheckCircle, CheckSquare, AlertCircle, Loader2, ChevronDown, ArrowUpDown, Wrench, Boxes, Cpu } from 'lucide-react';
import type { LaunchableTaskTypeOption } from '../taskCapabilities';
import type { ComputeExperimentForm } from '../hooks/useTaskModal';
import { COMPUTE_ACCELERATOR_OPTIONS, COMPUTE_EXPERIMENT_TASK_TYPE, canonicalMachineTypeForAccelerator } from '../hooks/useTaskModal';
import type { ComputeSuccessOperator } from '../api';

const TASK_TYPE_OPTION_ICONS: Record<string, typeof Plus> = {
  create: Plus,
  refactor: Wrench,
  plan: GitFork,
  research: Microscope,
  review: CheckCircle,
  panel_review: CheckSquare,
  fix: AlertCircle,
  migrate: ArrowUpDown,
  compute_experiment: Cpu,
};

const SUCCESS_OPERATORS: ComputeSuccessOperator[] = ['>=', '<=', '>', '<', '==', '!='];

// Fallback icon for backend-registered launchable types that have no bespoke
// frontend icon — keeps a dynamically-registered type renderable with no edit.
const DEFAULT_TASK_TYPE_ICON = Boxes;

interface NewTaskModalProps {
  newTaskRepo: string;
  newTaskBranch: string;
  newTaskTitle: string;
  newTaskPrompt: string;
  newTaskType: string;
  newTaskPrNumber: string;
  newTaskPrBranch: string;
  setNewTaskRepo: (value: string) => void;
  setNewTaskBranch: (value: string) => void;
  setNewTaskTitle: (value: string) => void;
  setNewTaskPrompt: (value: string) => void;
  setNewTaskType: (value: string) => void;
  setNewTaskPrNumber: (value: string) => void;
  setNewTaskPrBranch: (value: string) => void;
  newTaskModel: string;
  newTaskEffort: string;
  setNewTaskModel: (value: string) => void;
  setNewTaskEffort: (value: string) => void;
  computeExperiment: ComputeExperimentForm;
  setComputeExperiment: React.Dispatch<React.SetStateAction<ComputeExperimentForm>>;
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
  launchableTaskTypeOptions: LaunchableTaskTypeOption[];
  taskBranchDefaultsReady: boolean;
  taskBranchDefaultsLoading: boolean;
  taskBranchDefaultsError: string | null;
  integrationBranch?: string;
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
  computeExperiment,
  setComputeExperiment,
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
  launchableTaskTypeOptions,
  taskBranchDefaultsReady,
  taskBranchDefaultsLoading,
  taskBranchDefaultsError,
  integrationBranch,
  onRepoChange,
  onSubmit,
  onClose,
}: NewTaskModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [prNumberError, setPrNumberError] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const prNumberRef = useRef<HTMLInputElement>(null);

  const handleTaskTypeChange = (value: string) => {
    setNewTaskType(value);
    setNewTaskModel(getDefaultModelForType(value));
    setNewTaskEffort(getDefaultEffortForType(value));
  };

  const isPrNumberValid = (value: string) => /^[1-9]\d*$/.test(value);

  const isComputeExperiment = newTaskType === COMPUTE_EXPERIMENT_TASK_TYPE;
  const updateCompute = (field: keyof ComputeExperimentForm, value: string) =>
    // The cast is safe: every text field holds a string, and `successOperator`
    // is only ever set from the operator <select> whose options are the closed
    // ComputeSuccessOperator set.
    setComputeExperiment(f => ({ ...f, [field]: value }) as ComputeExperimentForm);
  const selectedCanonicalMachineType = canonicalMachineTypeForAccelerator(computeExperiment.accelerator);
  const handleComputeAcceleratorChange = (accelerator: string) => {
    setComputeExperiment(f => {
      const nextCanonical = canonicalMachineTypeForAccelerator(accelerator);
      return {
        ...f,
        accelerator,
        machineType: f.machineType === nextCanonical ? f.machineType : '',
      };
    });
  };
  // Client-side mirror of the backend launch-contract rules (non-empty command,
  // positive spend cap + deadline). Surfaced inline once a submit is attempted.
  const computeCommandInvalid = !computeExperiment.command.trim();
  const computeSpendCap = Number(computeExperiment.spendCapUsd);
  const computeSpendInvalid = !(Number.isFinite(computeSpendCap) && computeSpendCap > 0);
  const computeDeadline = Number(computeExperiment.deadlineSeconds);
  const computeDeadlineInvalid = !(Number.isInteger(computeDeadline) && computeDeadline > 0);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitAttempted(true);
    if (repoError || branchError) return;
    if (!taskBranchDefaultsReady) return;
    if ((newTaskType === 'review' || newTaskType === 'panel_review' || newTaskType === 'fix') && !isPrNumberValid(newTaskPrNumber)) {
      setPrNumberError(true);
      prNumberRef.current?.focus();
      return;
    }
    if (isComputeExperiment && (computeCommandInvalid || computeSpendInvalid || computeDeadlineInvalid)) {
      // Inline errors render via submitAttempted; don't round-trip a doomed launch.
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
          <button onClick={onClose} className="min-h-[44px] min-w-[44px] flex items-center justify-center -mr-2 text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-5">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Repository</label>
                {!repoLoading && (
                  <button
                    type="button"
                    onClick={() => { setUseManualRepo(m => !m); if (!useManualRepo) { setNewTaskRepo(''); setBranchOptions([]); } }}
                    className="min-h-[44px] sm:min-h-0 flex items-center text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
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
                    className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono ${submitAttempted && repoError ? 'border-red-500' : 'border-neutral-800'}`}
                    placeholder="owner/repo"
                  />
                  {repoError && <p className="text-[10px] text-amber-500 mt-1">{repoError}</p>}
                </>
              ) : (
                <select
                  required
                  value={newTaskRepo}
                  onChange={e => onRepoChange(e.target.value)}
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer ${submitAttempted && repoError ? 'border-red-500' : 'border-neutral-800'}`}
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
                    className="min-h-[44px] sm:min-h-0 flex items-center text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
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
                    className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono ${submitAttempted && branchError ? 'border-red-500' : 'border-neutral-800'}`}
                    placeholder={integrationBranch || "main"}
                  />
                  {branchError && <p className="text-[10px] text-amber-500 mt-1">{branchError}</p>}
                </>
              ) : (
                <select
                  value={newTaskBranch}
                  onChange={e => setNewTaskBranch(e.target.value)}
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer ${submitAttempted && branchError ? 'border-red-500' : 'border-neutral-800'}`}
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
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
              {launchableTaskTypeOptions.map(opt => {
                const Icon = TASK_TYPE_OPTION_ICONS[opt.value] ?? DEFAULT_TASK_TYPE_ICON;
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => handleTaskTypeChange(opt.value)}
                    className={`min-w-0 min-h-[44px] sm:min-h-0 flex items-center gap-2 px-3 py-2 rounded-lg border text-xs font-medium transition-all ${
                      newTaskType === opt.value
                        ? 'bg-white/5 border-neutral-600 text-neutral-100'
                        : 'bg-neutral-950 border-neutral-800 text-neutral-500 hover:border-neutral-700'
                    }`}
                  >
                    <Icon className="w-3.5 h-3.5 shrink-0" />
                    <div className="text-left min-w-0">
                      <div className="truncate">{opt.label}</div>
                      <div className="text-[10px] text-neutral-500 font-normal truncate">{opt.description}</div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
          <div className={`transition-all duration-150 ${(newTaskType === 'review' || newTaskType === 'panel_review' || newTaskType === 'fix') ? 'opacity-100 h-auto mb-4' : 'opacity-0 h-0 overflow-hidden'}`}>
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
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 ${prNumberError ? 'border-red-500' : 'border-neutral-800'}`}
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
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                  placeholder="e.g., blueprint/task-abc123"
                />
              </div>
            </div>
          </div>
          {isComputeExperiment && (
            <div className="mb-4 rounded-lg border border-neutral-800 bg-neutral-950/50 p-3">
              <div className="flex items-center gap-1.5 mb-3 text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">
                <Cpu className="w-3.5 h-3.5 text-neutral-500" />
                Compute Experiment Contract
              </div>
              <div className="mb-3">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Command</label>
                <textarea
                  value={computeExperiment.command}
                  onChange={e => updateCompute('command', e.target.value)}
                  className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 resize-y min-h-[64px] transition-all placeholder:text-neutral-600 ${submitAttempted && computeCommandInvalid ? 'border-red-500' : 'border-neutral-800'}`}
                  placeholder="python train.py --epochs 3"
                />
                {submitAttempted && computeCommandInvalid && <p className="text-[10px] text-red-500 mt-1">Command is required.</p>}
              </div>
              <div className="grid grid-cols-2 gap-3 mb-3">
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Accelerator <span className="text-neutral-600 normal-case font-normal tracking-normal">(optional)</span></label>
                  <select
                    value={computeExperiment.accelerator}
                    onChange={e => handleComputeAcceleratorChange(e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
                  >
                    <option value="">CPU (none)</option>
                    {COMPUTE_ACCELERATOR_OPTIONS.map(option => (
                      <option key={option.accelerator} value={option.accelerator}>
                        {option.accelerator}
                      </option>
                    ))}
                  </select>
                </div>
                {selectedCanonicalMachineType ? (
                  <div>
                    <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Machine Type <span className="text-neutral-600 normal-case font-normal tracking-normal">(optional)</span></label>
                    <select
                      value={computeExperiment.machineType}
                      onChange={e => updateCompute('machineType', e.target.value)}
                      className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
                    >
                      <option value="">Default canonical</option>
                      <option value={selectedCanonicalMachineType}>{selectedCanonicalMachineType}</option>
                    </select>
                  </div>
                ) : (
                  <div>
                    <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Machine Type <span className="text-neutral-600 normal-case font-normal tracking-normal">(optional)</span></label>
                    <div className="w-full bg-neutral-950 border border-neutral-800 text-neutral-600 rounded-lg px-3 py-2 text-base md:text-sm font-mono">
                      Default CPU
                    </div>
                  </div>
                )}
              </div>
              <div className="mb-3">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Provisioning <span className="text-neutral-600 normal-case font-normal tracking-normal">(SPOT saves ~60–90% for preemption-tolerant workloads)</span></label>
                <select
                  value={computeExperiment.provisioningModel}
                  onChange={e => updateCompute('provisioningModel', e.target.value as 'STANDARD' | 'SPOT')}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
                >
                  <option value="STANDARD">STANDARD (on-demand)</option>
                  <option value="SPOT">SPOT (preemptible, ~60–90% cheaper)</option>
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3 mb-3">
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Spend Cap (USD)</label>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={computeExperiment.spendCapUsd}
                    onChange={e => updateCompute('spendCapUsd', e.target.value)}
                    className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 ${submitAttempted && computeSpendInvalid ? 'border-red-500' : 'border-neutral-800'}`}
                    placeholder="e.g., 5.00"
                  />
                  {submitAttempted && computeSpendInvalid && <p className="text-[10px] text-red-500 mt-1">Spend cap must be positive.</p>}
                </div>
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Deadline (seconds)</label>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={computeExperiment.deadlineSeconds}
                    onChange={e => updateCompute('deadlineSeconds', e.target.value)}
                    className={`w-full bg-neutral-950 border text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 ${submitAttempted && computeDeadlineInvalid ? 'border-red-500' : 'border-neutral-800'}`}
                    placeholder="e.g., 1800"
                  />
                  {submitAttempted && computeDeadlineInvalid && <p className="text-[10px] text-red-500 mt-1">Deadline must be a positive integer.</p>}
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3 mb-3">
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Experiment <span className="text-neutral-600 normal-case font-normal tracking-normal">(optional)</span></label>
                  <input
                    type="text"
                    value={computeExperiment.experiment}
                    onChange={e => updateCompute('experiment', e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    placeholder="e.g., lora-sweep"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Benchmark <span className="text-neutral-600 normal-case font-normal tracking-normal">(optional)</span></label>
                  <input
                    type="text"
                    value={computeExperiment.benchmark}
                    onChange={e => updateCompute('benchmark', e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    placeholder="e.g., mmlu"
                  />
                </div>
              </div>
              <div className="mb-3">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Hypothesis <span className="text-neutral-600 normal-case font-normal tracking-normal">(optional)</span></label>
                <input
                  type="text"
                  value={computeExperiment.hypothesis}
                  onChange={e => updateCompute('hypothesis', e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                  placeholder="What measurable outcome do you expect?"
                />
              </div>
              <div>
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Success Criterion <span className="text-neutral-600 normal-case font-normal tracking-normal">(optional)</span></label>
                <div className="grid grid-cols-[1fr_auto_1fr] gap-2 items-center">
                  <input
                    type="text"
                    value={computeExperiment.successMetric}
                    onChange={e => updateCompute('successMetric', e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    placeholder="metric (e.g., accuracy)"
                  />
                  <select
                    value={computeExperiment.successOperator}
                    onChange={e => updateCompute('successOperator', e.target.value)}
                    className="bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-2 py-2 text-base md:text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
                  >
                    {SUCCESS_OPERATORS.map(op => (
                      <option key={op} value={op}>{op}</option>
                    ))}
                  </select>
                  <input
                    type="number"
                    step="any"
                    value={computeExperiment.successThreshold}
                    onChange={e => updateCompute('successThreshold', e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                    placeholder="threshold"
                  />
                </div>
                <p className="text-[10px] text-neutral-600 mt-1">Leave blank to launch without a machine-checkable goal.</p>
              </div>
              <div className="mt-3">
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={computeExperiment.requireLiveRelease}
                    onChange={e => setComputeExperiment(f => ({ ...f, requireLiveRelease: e.target.checked }))}
                    className="w-4 h-4 rounded border-neutral-700 bg-neutral-950 accent-neutral-400"
                  />
                  <span className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Require live release</span>
                  <span className="text-[11px] text-neutral-600 normal-case font-normal tracking-normal">(block until promoted release is serving-confirmed)</span>
                </label>
                <label className="mt-2 flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={computeExperiment.requireDurableArtifact}
                    onChange={e =>
                      setComputeExperiment(f => ({
                        ...f,
                        requireDurableArtifact: e.target.checked,
                      }))
                    }
                    className="w-4 h-4 rounded border-neutral-700 bg-neutral-950 accent-neutral-400"
                  />
                  <span className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Require durable artifact</span>
                  <span className="text-[11px] text-neutral-600 normal-case font-normal tracking-normal">
                    (block completion unless a durable-class gs://, s3://, az://,
                    or file: COMPUTE_RESULT artifact is delivered)
                  </span>
                </label>
              </div>
            </div>
          )}
          <div className="mb-4">
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">
              Task Title{newTaskType === 'panel_review' && <span className="ml-1 text-neutral-600 normal-case font-normal tracking-normal">(optional)</span>}
            </label>
            <input
              type="text"
              required={newTaskType !== 'panel_review' && !isComputeExperiment}
              autoFocus
              value={newTaskTitle}
              onChange={e => setNewTaskTitle(e.target.value)}
              className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
              placeholder="e.g., Add user authentication"
            />
          </div>
          <div className="mb-6">
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">
              Prompt / Instructions{newTaskType === 'panel_review' && <span className="ml-1 text-neutral-600 normal-case font-normal tracking-normal">(optional — fixed workflow)</span>}
            </label>
            <div className="relative">
              <textarea
                required={newTaskType !== 'panel_review' && !isComputeExperiment}
                value={newTaskPrompt}
                onChange={e => setNewTaskPrompt(e.target.value)}
                className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 resize-y min-h-[160px] max-h-[400px] transition-all placeholder:text-neutral-600"
                placeholder="Describe what Claude Code should do in this repository. Be specific about files, patterns, and expected outcomes..."
              />
              <span className="text-[10px] text-neutral-600 absolute bottom-2 right-2 pointer-events-none">{newTaskPrompt.length} chars</span>
            </div>
          </div>
          <div className="mb-4">
            <button
              type="button"
              onClick={() => setAdvancedOpen(o => !o)}
              className="min-h-[44px] sm:min-h-0 flex items-center gap-1 text-[11px] font-semibold text-neutral-500 hover:text-neutral-300 uppercase tracking-wider transition-colors"
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
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
                  >
                    <option value="">Default</option>
                    {/* TEMPORARY: fable-5 limited availability ~2 weeks from 2026-06-09 */}
                    <option value="claude-fable-5">claude-fable-5</option>
                    <option value="claude-opus-4-8">claude-opus-4-8</option>
                    <option value="claude-opus-4-7">claude-opus-4-7</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Effort</label>
                  <select
                    value={newTaskEffort}
                    onChange={e => setNewTaskEffort(e.target.value)}
                    className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all appearance-none cursor-pointer"
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
            {submitAttempted && (repoError || branchError || taskBranchDefaultsError) && (
              <span className="text-[11px] text-red-400 mr-auto">Fix errors above before launching.</span>
            )}
            {!taskBranchDefaultsReady && (
              <span className="text-[11px] text-amber-400 mr-auto">
                {taskBranchDefaultsLoading ? 'Loading project branch config...' : taskBranchDefaultsError || 'Project branch config unavailable.'}
              </span>
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
              disabled={submitting || !taskBranchDefaultsReady}
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
