import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import userEvent from '@testing-library/user-event';
import { NewTaskModal } from './NewTaskModal';
import type { AcceptanceEvidenceForm, ComputeExperimentForm } from '../hooks/useTaskModal';
import { defaultAcceptanceEvidenceForm, defaultComputeExperimentForm } from '../hooks/useTaskModal';
import { usePersistedState } from '../hooks/usePersistedState';

const mockDraftComputeExperiment = vi.fn();

vi.mock('../api', () => ({
  draftComputeExperiment: (...args: unknown[]) => mockDraftComputeExperiment(...args),
  API_BASE_URL: '',
}));

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('tok'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

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
  ideaText: '',
  setIdeaText: vi.fn(),
  draftNotes: null,
  setDraftNotes: vi.fn(),
  onDraftApplied: vi.fn(),
  onRepoChange: vi.fn(),
  onSubmit: vi.fn(),
  onClose: vi.fn(),
  acceptanceEvidence: defaultAcceptanceEvidenceForm(),
  setAcceptanceEvidence: vi.fn(),
};

function makeComputeExperiment(overrides?: Partial<ComputeExperimentForm>): ComputeExperimentForm {
  return { ...defaultComputeExperimentForm(), ...overrides };
}

// Stateful wrapper that manages ideaText/draftNotes so the textarea behaves
// as a controlled input in tests (mirrors what useTaskModal does in production).
// Uses usePersistedState to match hook behavior, enabling the persist-across-
// remounts test to work correctly.
function DraftStateWrapper(props: Omit<React.ComponentProps<typeof NewTaskModal>, 'ideaText' | 'setIdeaText' | 'draftNotes' | 'setDraftNotes'>) {
  const [ideaText, setIdeaText] = usePersistedState('compute-experiment-idea', '');
  const [draftNotes, setDraftNotes] = usePersistedState<string | null>('compute-experiment-draft-notes', null);
  return (
    <NewTaskModal
      {...props}
      ideaText={ideaText}
      setIdeaText={setIdeaText}
      draftNotes={draftNotes}
      setDraftNotes={setDraftNotes}
    />
  );
}

describe('NewTaskModal — Draft contract', () => {
  let computeExperiment: ComputeExperimentForm;
  let setComputeExperiment: ReturnType<typeof vi.fn> & React.Dispatch<React.SetStateAction<ComputeExperimentForm>>;

  beforeEach(() => {
    vi.clearAllMocks();
    computeExperiment = makeComputeExperiment();
    setComputeExperiment = vi.fn().mockImplementation((updater: unknown) => {
      if (typeof updater === 'function') {
        computeExperiment = (updater as (prev: ComputeExperimentForm) => ComputeExperimentForm)(computeExperiment);
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
      <DraftStateWrapper
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
      <DraftStateWrapper
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

  it('shows server-provided notes as the inline error when draft_available=false', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment.mockResolvedValue({
      draft_available: false,
      notes: 'Idea is too vague to draft a contract — add a metric.',
    });
    const setComputeExperimentSpy = vi.fn();

    render(
      <DraftStateWrapper
        {...baseProps}
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={setComputeExperimentSpy}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'something');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => {
      expect(screen.getByText('Idea is too vague to draft a contract — add a metric.')).toBeInTheDocument();
    });
    // server notes replace the generic fallback message
    expect(screen.queryByText(/draft not available — fill the form manually/i)).not.toBeInTheDocument();
    expect(setComputeExperimentSpy).not.toHaveBeenCalled();
  });

  it('renders notes from a successful draft as context without an error', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment.mockResolvedValue({
      draft_available: true,
      command: 'python train.py',
      notes: 'Assumed single-GPU since the idea did not mention scale.',
    });

    render(
      <DraftStateWrapper
        {...baseProps}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'tune lr');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    const notes = await screen.findByText('Assumed single-GPU since the idea did not mention scale.');
    // rendered as neutral context, not the red draftError paragraph
    expect(notes).toHaveClass('text-neutral-400');
    expect(notes).not.toHaveClass('text-red-400');
    expect(computeExperiment.command).toBe('python train.py');
  });

  it('clears stale notes when a later successful draft omits notes', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment
      .mockResolvedValueOnce({
        draft_available: true,
        command: 'python train.py --v1',
        notes: 'First-draft caveat.',
      })
      .mockResolvedValueOnce({
        draft_available: true,
        command: 'python train.py --v2',
      });

    render(
      <DraftStateWrapper
        {...baseProps}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'tune lr');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));
    await screen.findByText('First-draft caveat.');

    await user.click(screen.getByRole('button', { name: /draft contract/i }));
    await waitFor(() => {
      expect(screen.queryByText('First-draft caveat.')).not.toBeInTheDocument();
    });
  });

  it('surfaces network error inline without clearing existing form state', async () => {
    const user = userEvent.setup();
    mockDraftComputeExperiment.mockRejectedValue(new Error('Network error'));
    const prefilled = makeComputeExperiment({ command: 'python existing.py' });
    const setComputeExperimentSpy = vi.fn();

    render(
      <DraftStateWrapper
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
      <DraftStateWrapper
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

  it('persists research idea and draft notes across component remounts', async () => {
    const user = userEvent.setup();

    // Clear localStorage before test to ensure clean state
    localStorage.removeItem('blueprint-admin:compute-experiment-idea');
    localStorage.removeItem('blueprint-admin:compute-experiment-draft-notes');
    localStorage.removeItem('blueprint-admin:compute-experiment-last-draft');

    const draftResponse = {
      draft_available: true,
      command: 'python train.py --lr 0.01',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
      notes: 'Assumed single-GPU configuration',
    };
    mockDraftComputeExperiment.mockResolvedValue(draftResponse);

    // First render: type the idea and draft
    const { unmount } = render(
      <DraftStateWrapper
        {...baseProps}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    const ideaInput = screen.getByPlaceholderText(/describe your experiment/i);
    await user.type(ideaInput, 'Tune learning rate on ImageNet');

    // Click draft to populate the form and set draft notes
    await user.click(screen.getByRole('button', { name: /draft contract/i }));
    await waitFor(() => {
      expect(screen.getByText('Assumed single-GPU configuration')).toBeInTheDocument();
    });

    // Verify that localStorage has the persisted values
    const storedIdea = localStorage.getItem('blueprint-admin:compute-experiment-idea');
    expect(storedIdea).toBe(JSON.stringify('Tune learning rate on ImageNet'));

    // Unmount the component
    unmount();

    // Second render: component should restore the persisted idea and notes
    mockDraftComputeExperiment.mockResolvedValue(draftResponse);
    render(
      <DraftStateWrapper
        {...baseProps}
        computeExperiment={computeExperiment}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    // Verify the idea is restored from localStorage
    expect(screen.getByPlaceholderText(/describe your experiment/i)).toHaveValue('Tune learning rate on ImageNet');
    // Verify the draft notes are restored
    expect(screen.getByText('Assumed single-GPU configuration')).toBeInTheDocument();
  });

  it('shows which fields were updated when a draft changes multiple values', async () => {
    const user = userEvent.setup();
    const prefilled = makeComputeExperiment({
      command: 'python old_command.py',
      spendCapUsd: '10',
      deadlineSeconds: '3600',
      hypothesis: 'Old hypothesis',
    });

    mockDraftComputeExperiment.mockResolvedValue({
      draft_available: true,
      command: 'python new_command.py --lr 0.01',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
      hypothesis: 'New hypothesis for improved results',
      notes: 'Adjusted resource requirements based on analysis.',
    });

    render(
      <DraftStateWrapper
        {...baseProps}
        computeExperiment={prefilled}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'Refine training');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    // Verify that the changes are displayed
    await waitFor(() => {
      expect(screen.getByText(/Updated: Command, Spend cap, Deadline, Hypothesis/)).toBeInTheDocument();
    });

    // Verify the values were actually updated
    expect(computeExperiment.command).toBe('python new_command.py --lr 0.01');
    expect(computeExperiment.spendCapUsd).toBe('5');
    expect(computeExperiment.deadlineSeconds).toBe('1800');
    expect(computeExperiment.hypothesis).toBe('New hypothesis for improved results');
  });

  it('does not show changes when draft values match current form state', async () => {
    const user = userEvent.setup();
    const prefilled = makeComputeExperiment({
      command: 'python train.py --lr 0.01',
      spendCapUsd: '5',
      deadlineSeconds: '1800',
      hypothesis: 'Test hypothesis',
    });

    mockDraftComputeExperiment.mockResolvedValue({
      draft_available: true,
      command: 'python train.py --lr 0.01',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
      hypothesis: 'Test hypothesis',
      notes: 'Form already matches the optimal contract.',
    });

    render(
      <DraftStateWrapper
        {...baseProps}
        computeExperiment={prefilled}
        setComputeExperiment={setComputeExperiment}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'Verify form');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => {
      expect(screen.getByText(/Form already matches the optimal contract/)).toBeInTheDocument();
    });

    // Verify that no "Updated:" message appears
    expect(screen.queryByText(/^Updated:/)).not.toBeInTheDocument();
  });

  it('calls onDraftApplied when a successful draft is applied', async () => {
    const user = userEvent.setup();
    const onDraftApplied = vi.fn();
    mockDraftComputeExperiment.mockResolvedValue({
      draft_available: true,
      command: 'python train.py',
      spend_cap_usd: 5,
      deadline_seconds: 1800,
    });

    render(
      <DraftStateWrapper
        {...baseProps}
        onDraftApplied={onDraftApplied}
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'My idea');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => expect(onDraftApplied).toHaveBeenCalledTimes(1));
  });

  it('does not call onDraftApplied when draft_available is false', async () => {
    const user = userEvent.setup();
    const onDraftApplied = vi.fn();
    mockDraftComputeExperiment.mockResolvedValue({ draft_available: false });

    render(
      <DraftStateWrapper
        {...baseProps}
        onDraftApplied={onDraftApplied}
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
      />,
    );

    await user.type(screen.getByPlaceholderText(/describe your experiment/i), 'something');
    await user.click(screen.getByRole('button', { name: /draft contract/i }));

    await waitFor(() => expect(screen.getByText(/draft not available/i)).toBeInTheDocument());
    expect(onDraftApplied).not.toHaveBeenCalled();
  });
});

describe('NewTaskModal — Acceptance Evidence', () => {
  function makeValidEvidence(): AcceptanceEvidenceForm {
    const form = defaultAcceptanceEvidenceForm();
    form.testPath = 'tests/test_feature.py';
    return form;
  }

  it('hides the Acceptance Evidence section for non-code-producing task types', () => {
    render(
      <NewTaskModal
        {...baseProps}
        newTaskType="plan"
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
      />,
    );
    expect(screen.queryByText('Acceptance Evidence')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('e.g., tests/test_foo.py')).not.toBeInTheDocument();
  });

  it('shows the Acceptance Evidence section for code-producing task types', () => {
    render(
      <NewTaskModal
        {...baseProps}
        newTaskType="create"
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
      />,
    );
    expect(screen.getByText('Acceptance Evidence')).toBeInTheDocument();
    // named_test is the default kind, so the Test Path field renders.
    expect(screen.getByPlaceholderText('e.g., tests/test_foo.py')).toBeInTheDocument();
  });

  it('switching the evidence kind renders the corresponding fields', async () => {
    const user = userEvent.setup();
    function EvidenceStateWrapper(props: Omit<React.ComponentProps<typeof NewTaskModal>, 'acceptanceEvidence' | 'setAcceptanceEvidence'>) {
      const [evidence, setEvidence] = React.useState<AcceptanceEvidenceForm>(defaultAcceptanceEvidenceForm());
      return (
        <NewTaskModal
          {...props}
          acceptanceEvidence={evidence}
          setAcceptanceEvidence={setEvidence}
        />
      );
    }
    render(
      <EvidenceStateWrapper
        {...baseProps}
        newTaskType="create"
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
      />,
    );
    await user.selectOptions(screen.getByDisplayValue(/Named test/), 'observable_behavior');
    expect(screen.getByPlaceholderText(/describe a concrete behavior/i)).toBeInTheDocument();
  });

  it('blocks submit with an inline error when acceptance evidence is invalid', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <NewTaskModal
        {...baseProps}
        newTaskType="create"
        newTaskTitle="Build feature"
        newTaskPrompt="Make it work"
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
        onSubmit={onSubmit}
      />,
    );
    // Default evidence has an empty test path — invalid for named_test.
    await user.click(screen.getByRole('button', { name: 'Launch' }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/name a real test file/i)).toBeInTheDocument();
  });

  it('calls onSubmit when acceptance evidence is valid', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <NewTaskModal
        {...baseProps}
        newTaskType="create"
        newTaskTitle="Build feature"
        newTaskPrompt="Make it work"
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
        acceptanceEvidence={makeValidEvidence()}
        onSubmit={onSubmit}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Launch' }));
    expect(onSubmit).toHaveBeenCalled();
  });
});

describe('NewTaskModal — submit surface', () => {
  it('submits successfully without acceptance_evidence and surfaces the reason on 422', async () => {
    const user = userEvent.setup();
    const options = [
      { value: 'plan', label: 'Plan', description: '' },
      { value: 'create', label: 'Create', description: '' },
    ];
    const validEvidence = { ...defaultAcceptanceEvidenceForm(), testPath: 'tests/test_feature.py' };

    // Success path: a non-code-producing task type submits with no
    // acceptance_evidence declaration (no evidence section renders).
    const successOnSubmit = vi.fn().mockResolvedValue(undefined);
    const { unmount } = render(
      <NewTaskModal
        {...baseProps}
        newTaskType="plan"
        newTaskTitle="Build the roadmap"
        newTaskPrompt="Decompose the work into a plan."
        launchableTaskTypeOptions={options}
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
        onSubmit={successOnSubmit}
      />,
    );
    expect(screen.queryByText('Acceptance Evidence')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Launch' }));
    expect(successOnSubmit).toHaveBeenCalledTimes(1);
    unmount();

    // 422 path: the server's actual validation reason renders inline in the
    // modal instead of a blanket 'Invalid request'.
    const reason =
      "Payload of code-producing task type 'create' requires an 'acceptance_evidence' declaration";
    const failingOnSubmit = vi.fn().mockRejectedValue(new Error(reason));
    render(
      <NewTaskModal
        {...baseProps}
        newTaskType="create"
        newTaskTitle="Build the feature"
        newTaskPrompt="Implement the change."
        launchableTaskTypeOptions={options}
        computeExperiment={makeComputeExperiment()}
        setComputeExperiment={vi.fn()}
        acceptanceEvidence={validEvidence}
        onSubmit={failingOnSubmit}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Launch' }));
    expect(failingOnSubmit).toHaveBeenCalledTimes(1);
    expect(screen.getByText(reason)).toBeInTheDocument();
  });
});
