import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NewTaskModal } from './NewTaskModal';
import type { ComputeExperimentForm } from '../hooks/useTaskModal';
import { defaultComputeExperimentForm } from '../hooks/useTaskModal';

const mockDraftComputeExperiment = vi.fn();

vi.mock('../api', () => ({
  draftComputeExperiment: (...args: unknown[]) => mockDraftComputeExperiment(...args),
  API_BASE_URL: '',
}));

vi.mock('../firebase', () => ({ getIdToken: vi.fn().mockResolvedValue('tok') }));

const baseProps = {
  newTaskRepo: 'owner/repo',
  newTaskBranch: 'main',
  newTaskTitle: '',
  newTaskPrompt: '',
  newTaskType: 'compute_experiment',
  newTaskPrNumber: '',
  newTaskPrBranch: '',
  newTaskModel: '',
  newTaskEffort: '',
  setNewTaskRepo: vi.fn(),
  setNewTaskBranch: vi.fn(),
  setNewTaskTitle: vi.fn(),
  setNewTaskPrompt: vi.fn(),
  setNewTaskType: vi.fn(),
  setNewTaskPrNumber: vi.fn(),
  setNewTaskPrBranch: vi.fn(),
  setNewTaskModel: vi.fn(),
  setNewTaskEffort: vi.fn(),
  getDefaultModelForType: vi.fn().mockReturnValue(''),
  getDefaultEffortForType: vi.fn().mockReturnValue(''),
  repoOptions: [{ full_name: 'owner/repo', default_branch: 'main' }],
  repoLoading: false,
  repoError: null,
  useManualRepo: false,
  setUseManualRepo: vi.fn(),
  branchOptions: ['main'],
  branchLoading: false,
  branchError: null,
  useManualBranch: false,
  setUseManualBranch: vi.fn(),
  setBranchOptions: vi.fn(),
  launchableTaskTypeOptions: [{ value: 'compute_experiment', label: 'Compute Experiment', description: '' }],
  taskBranchDefaultsReady: true,
  taskBranchDefaultsLoading: false,
  taskBranchDefaultsError: null,
  preflightResult: null,
  preflightLoading: false,
  preflightError: null,
  onRepoChange: vi.fn(),
  onSubmit: vi.fn(),
  onClose: vi.fn(),
};

function makeComputeExperiment(overrides?: Partial<ComputeExperimentForm>): ComputeExperimentForm {
  return { ...defaultComputeExperimentForm(), ...overrides };
}

describe('NewTaskModal — Draft contract', () => {
  let computeExperiment: ComputeExperimentForm;
  let setComputeExperiment: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    computeExperiment = makeComputeExperiment();
    setComputeExperiment = vi.fn().mockImplementation((updater: unknown) => {
      if (typeof updater === 'function') {
        computeExperiment = updater(computeExperiment);
      } else {
        computeExperiment = updater as ComputeExperimentForm;
      }
    });
  });

  it('shows Research Idea textarea and Draft contract button in compute_experiment mode', () => {
    render(
      <NewTaskModal
        {...baseProps}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );
    expect(screen.getByPlaceholderText(/describe your experiment/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /draft contract/i })).toBeInTheDocument();
  });

  it('Draft contract button is disabled when idea textarea is empty', () => {
    render(
      <NewTaskModal
        {...baseProps}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );
    expect(screen.getByRole('button', { name: /draft contract/i })).toBeDisabled();
  });

  it('calls draftComputeExperiment and populates form fields on success', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment.mockResolvedValue({
      draft_available: true,
      title: 'Test experiment',
      prompt: 'Run training sweep',
      command: 'python train.py --lr 0.01',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
      hypothesis: 'Lower LR improves accuracy',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
    });
    const setNewTaskTitle = vi.fn();
    const setNewTaskPrompt = vi.fn();

    render(
      <NewTaskModal
        {...baseProps}
        setNewTaskTitle={setNewTaskTitle}
        setNewTaskPrompt={setNewTaskPrompt}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'Fine-tune LR on ImageNet');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => {
      expect(mockDraftComputeExperiment).toHaveBeenCalledWith({
        idea: 'Fine-tune LR on ImageNet',
        github_repo: 'owner/repo',
        base_branch: 'main',
      });
    });

    await waitFor(() => expect(setNewTaskTitle).toHaveBeenCalledWith('Test experiment'));
    expect(setNewTaskPrompt).toHaveBeenCalledWith('Run training sweep');
    // setComputeExperiment was called; verify the resulting form state
    expect(computeExperiment.command).toBe('python train.py --lr 0.01');
    expect(computeExperiment.spendCapUsd).toBe('5');
    expect(computeExperiment.deadlineSeconds).toBe('1800');
    expect(computeExperiment.hypothesis).toBe('Lower LR improves accuracy');
    expect(computeExperiment.successMetric).toBe('accuracy');
    expect(computeExperiment.successOperator).toBe('>=');
    expect(computeExperiment.successThreshold).toBe('0.9');
  });

  it('shows draft_available=false as inline error without clearing form', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment.mockResolvedValue({ draft_available: false });
    const prefilled = makeComputeExperiment({ command: 'python existing.py' });
    const setComputeExperimentSpy = vi.fn();

    render(
      <NewTaskModal
        {...baseProps}
        computeExperiment={prefilled}
        setComputeExperiment={setComputeExperimentSpy}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'something');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => {
      expect(screen.getByText(/draft not available/i)).toBeInTheDocument();
    });
    // form must not be touched
    expect(setComputeExperimentSpy).not.toHaveBeenCalled();
  });

  it('surfaces network error inline without clearing existing form state', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment.mockRejectedValue(new Error('Network error'));
    const prefilled = makeComputeExperiment({ command: 'python existing.py' });
    const setComputeExperimentSpy = vi.fn();

    render(
      <NewTaskModal
        {...baseProps}
        computeExperiment={prefilled}
        setComputeExperiment={setComputeExperimentSpy}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'test idea');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeInTheDocument();
    });
    expect(setComputeExperimentSpy).not.toHaveBeenCalled();
  });

  it('multi-arm draft populates arms and switches to arms mode', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment.mockResolvedValue({
      draft_available: true,
      arms: [
        { name: 'arm-A', command: 'python train.py --lr 0.01' },
        { name: 'arm-B', command: 'python train.py --lr 0.001' },
      ],
      declared_metric: 'accuracy',
      spend_cap_usd: 10,
      deadline_seconds: 3600,
    });

    render(
      <NewTaskModal
        {...baseProps}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'compare two LRs');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => expect(setComputeExperiment).toHaveBeenCalled());
    expect(computeExperiment.useArmsMode).toBe(true);
    expect(computeExperiment.arms).toHaveLength(2);
    expect(computeExperiment.arms[0].name).toBe('arm-A');
    expect(computeExperiment.declaredMetric).toBe('accuracy');
  });
});
