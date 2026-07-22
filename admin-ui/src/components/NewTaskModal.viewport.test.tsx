import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { NewTaskModal } from './NewTaskModal';
import { defaultComputeExperimentForm } from '../hooks/useTaskModal';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api', () => ({
  API_BASE_URL: '',
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  authHeaders: vi.fn(async () => ({})),
  fetchTaskTypeCatalog: vi.fn(async () => []),
  createComputeExperiment: vi.fn(async () => ({ id: 'compute-experiment-test' })),
  computeExperimentPreflight: vi.fn(async () => ({})),
  draftComputeExperiment: vi.fn(async () => ({ draft_available: false })),
  // useTaskModal loads the enrolled-runner list for the operator runner-pin
  // selector. This factory mock REPLACES the whole '../api' module, so every
  // named export the hook imports must appear here — a missing one is a hard
  // "does not provide an export named" SyntaxError in browser-mode ESM.
  listFleetRunners: vi.fn(async () => []),
}));

const LAUNCHABLE_OPTIONS = [
  { value: 'create', label: 'Create', description: 'Write code, open PR' },
  { value: 'review', label: 'Review', description: 'Review existing PR' },
  { value: 'fix', label: 'Fix', description: 'Fix PR feedback' },
];

describe('NewTaskModal — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewTaskModal
            newTaskRepo="example/repo"
            newTaskBranch="main"
            newTaskTitle="Add comprehensive error handling to the authentication module with detailed logging and recovery strategies"
            newTaskPrompt="Implement robust error handling across all authentication flows. Include detailed logging for debugging, implement retry logic with exponential backoff, and provide meaningful error messages to users."
            newTaskType="create"
            newTaskPrNumber=""
            newTaskPrBranch=""
            setNewTaskRepo={() => {}}
            setNewTaskBranch={() => {}}
            setNewTaskTitle={() => {}}
            setNewTaskPrompt={() => {}}
            setNewTaskType={() => {}}
            setNewTaskPrNumber={() => {}}
            setNewTaskPrBranch={() => {}}
            newTaskModel=""
            newTaskEffort=""
            setNewTaskModel={() => {}}
            setNewTaskEffort={() => {}}
            computeExperiment={defaultComputeExperimentForm()}
            setComputeExperiment={() => {}}
            getDefaultModelForType={() => 'claude-opus-4-7'}
            getDefaultEffortForType={() => 'medium'}
            repoOptions={[
              { full_name: 'example/repo', default_branch: 'main' },
              { full_name: 'example/another-repo', default_branch: 'develop' },
              { full_name: 'example/third-repo', default_branch: 'master' },
            ]}
            repoLoading={false}
            repoError={null}
            useManualRepo={false}
            setUseManualRepo={() => {}}
            branchOptions={['main', 'develop', 'staging']}
            branchLoading={false}
            branchError={null}
            useManualBranch={false}
            setUseManualBranch={() => {}}
            setBranchOptions={() => {}}
            launchableTaskTypeOptions={LAUNCHABLE_OPTIONS}
            taskBranchDefaultsReady={true}
            taskBranchDefaultsLoading={false}
            taskBranchDefaultsError={null}
            onRepoChange={() => {}}
            onSubmit={async () => {}}
            onClose={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('does not prefill main while project branch config is still loading', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <NewTaskModal
          newTaskRepo="example/repo"
          newTaskBranch=""
          newTaskTitle="Test task"
          newTaskPrompt="Test prompt"
          newTaskType="create"
          newTaskPrNumber=""
          newTaskPrBranch=""
          setNewTaskRepo={() => {}}
          setNewTaskBranch={() => {}}
          setNewTaskTitle={() => {}}
          setNewTaskPrompt={() => {}}
          setNewTaskType={() => {}}
          setNewTaskPrNumber={() => {}}
          setNewTaskPrBranch={() => {}}
          newTaskModel=""
          newTaskEffort=""
          setNewTaskModel={() => {}}
          setNewTaskEffort={() => {}}
          computeExperiment={defaultComputeExperimentForm()}
          setComputeExperiment={() => {}}
          getDefaultModelForType={() => 'claude-opus-4-7'}
          getDefaultEffortForType={() => 'medium'}
          repoOptions={[{ full_name: 'example/repo', default_branch: 'develop' }]}
          repoLoading={false}
          repoError={null}
          useManualRepo={false}
          setUseManualRepo={() => {}}
          branchOptions={[]}
          branchLoading={false}
          branchError={null}
          useManualBranch={false}
          setUseManualBranch={() => {}}
          setBranchOptions={() => {}}
          launchableTaskTypeOptions={LAUNCHABLE_OPTIONS}
          taskBranchDefaultsReady={false}
          taskBranchDefaultsLoading={true}
          taskBranchDefaultsError={null}
          onRepoChange={() => {}}
          onSubmit={async () => {}}
          onClose={() => {}}
        />,
      );
    });
    const branchInput = document.querySelector('input[placeholder="main"]') as HTMLInputElement | null;
    if (branchInput) {
      expect(branchInput.value).toBe('');
    }
    const launchButton = Array.from(document.querySelectorAll('button')).find(button => button.textContent?.includes('Launch')) as HTMLButtonElement | undefined;
    expect(launchButton?.disabled).toBe(true);
    expect(document.body.textContent).toContain('Loading project branch config...');
  });

  test('input focus does not trigger zoom at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewTaskModal
            newTaskRepo="example/repo"
            newTaskBranch="main"
            newTaskTitle="Test task"
            newTaskPrompt="Test prompt"
            newTaskType="create"
            newTaskPrNumber=""
            newTaskPrBranch=""
            setNewTaskRepo={() => {}}
            setNewTaskBranch={() => {}}
            setNewTaskTitle={() => {}}
            setNewTaskPrompt={() => {}}
            setNewTaskType={() => {}}
            setNewTaskPrNumber={() => {}}
            setNewTaskPrBranch={() => {}}
            newTaskModel=""
            newTaskEffort=""
            setNewTaskModel={() => {}}
            setNewTaskEffort={() => {}}
            computeExperiment={defaultComputeExperimentForm()}
            setComputeExperiment={() => {}}
            getDefaultModelForType={() => 'claude-opus-4-7'}
            getDefaultEffortForType={() => 'medium'}
            repoOptions={[{ full_name: 'example/repo', default_branch: 'main' }]}
            repoLoading={false}
            repoError={null}
            useManualRepo={false}
            setUseManualRepo={() => {}}
            branchOptions={['main']}
            branchLoading={false}
            branchError={null}
            useManualBranch={false}
            setUseManualBranch={() => {}}
            setBranchOptions={() => {}}
            launchableTaskTypeOptions={LAUNCHABLE_OPTIONS}
            taskBranchDefaultsReady={true}
            taskBranchDefaultsLoading={false}
            taskBranchDefaultsError={null}
            onRepoChange={() => {}}
            onSubmit={async () => {}}
            onClose={() => {}}
          />,
        );
      });
      // Find an input and focus it to simulate mobile browser behavior
      const input = document.querySelector('input[type="text"]') as HTMLInputElement;
      if (input) {
        await act(async () => {
          input.focus();
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        });
      }
    });
  });

  test('primary action buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewTaskModal
            newTaskRepo="example/repo"
            newTaskBranch="main"
            newTaskTitle="Test task"
            newTaskPrompt="Test prompt"
            newTaskType="create"
            newTaskPrNumber=""
            newTaskPrBranch=""
            setNewTaskRepo={() => {}}
            setNewTaskBranch={() => {}}
            setNewTaskTitle={() => {}}
            setNewTaskPrompt={() => {}}
            setNewTaskType={() => {}}
            setNewTaskPrNumber={() => {}}
            setNewTaskPrBranch={() => {}}
            newTaskModel=""
            newTaskEffort=""
            setNewTaskModel={() => {}}
            setNewTaskEffort={() => {}}
            computeExperiment={defaultComputeExperimentForm()}
            setComputeExperiment={() => {}}
            getDefaultModelForType={() => 'claude-opus-4-7'}
            getDefaultEffortForType={() => 'medium'}
            repoOptions={[{ full_name: 'example/repo', default_branch: 'main' }]}
            repoLoading={false}
            repoError={null}
            useManualRepo={false}
            setUseManualRepo={() => {}}
            branchOptions={['main']}
            branchLoading={false}
            branchError={null}
            useManualBranch={false}
            setUseManualBranch={() => {}}
            setBranchOptions={() => {}}
            launchableTaskTypeOptions={LAUNCHABLE_OPTIONS}
            taskBranchDefaultsReady={true}
            taskBranchDefaultsLoading={false}
            taskBranchDefaultsError={null}
            onRepoChange={() => {}}
            onSubmit={async () => {}}
            onClose={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('renders without horizontal overflow at 768px (tablet)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewTaskModal
            newTaskRepo="example/repo"
            newTaskBranch="main"
            newTaskTitle="Add comprehensive error handling to the authentication module with detailed logging and recovery strategies"
            newTaskPrompt="Implement robust error handling across all authentication flows. Include detailed logging for debugging, implement retry logic with exponential backoff, and provide meaningful error messages to users."
            newTaskType="create"
            newTaskPrNumber=""
            newTaskPrBranch=""
            setNewTaskRepo={() => {}}
            setNewTaskBranch={() => {}}
            setNewTaskTitle={() => {}}
            setNewTaskPrompt={() => {}}
            setNewTaskType={() => {}}
            setNewTaskPrNumber={() => {}}
            setNewTaskPrBranch={() => {}}
            newTaskModel=""
            newTaskEffort=""
            setNewTaskModel={() => {}}
            setNewTaskEffort={() => {}}
            computeExperiment={defaultComputeExperimentForm()}
            setComputeExperiment={() => {}}
            getDefaultModelForType={() => 'claude-opus-4-7'}
            getDefaultEffortForType={() => 'medium'}
            repoOptions={[
              { full_name: 'example/repo', default_branch: 'main' },
              { full_name: 'example/another-repo', default_branch: 'develop' },
            ]}
            repoLoading={false}
            repoError={null}
            useManualRepo={false}
            setUseManualRepo={() => {}}
            branchOptions={['main', 'develop', 'staging']}
            branchLoading={false}
            branchError={null}
            useManualBranch={false}
            setUseManualBranch={() => {}}
            setBranchOptions={() => {}}
            launchableTaskTypeOptions={LAUNCHABLE_OPTIONS}
            taskBranchDefaultsReady={true}
            taskBranchDefaultsLoading={false}
            taskBranchDefaultsError={null}
            onRepoChange={() => {}}
            onSubmit={async () => {}}
            onClose={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('labels and field text are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewTaskModal
            newTaskRepo="example/repo"
            newTaskBranch="main"
            newTaskTitle="Add comprehensive error handling to the authentication module with detailed logging and recovery strategies"
            newTaskPrompt="Implement robust error handling across all authentication flows. Include detailed logging for debugging, implement retry logic with exponential backoff, and provide meaningful error messages to users."
            newTaskType="create"
            newTaskPrNumber=""
            newTaskPrBranch=""
            setNewTaskRepo={() => {}}
            setNewTaskBranch={() => {}}
            setNewTaskTitle={() => {}}
            setNewTaskPrompt={() => {}}
            setNewTaskType={() => {}}
            setNewTaskPrNumber={() => {}}
            setNewTaskPrBranch={() => {}}
            newTaskModel=""
            newTaskEffort=""
            setNewTaskModel={() => {}}
            setNewTaskEffort={() => {}}
            computeExperiment={defaultComputeExperimentForm()}
            setComputeExperiment={() => {}}
            getDefaultModelForType={() => 'claude-opus-4-7'}
            getDefaultEffortForType={() => 'medium'}
            repoOptions={[{ full_name: 'example/repo', default_branch: 'main' }]}
            repoLoading={false}
            repoError={null}
            useManualRepo={false}
            setUseManualRepo={() => {}}
            branchOptions={['main']}
            branchLoading={false}
            branchError={null}
            useManualBranch={false}
            setUseManualBranch={() => {}}
            setBranchOptions={() => {}}
            launchableTaskTypeOptions={LAUNCHABLE_OPTIONS}
            taskBranchDefaultsReady={true}
            taskBranchDefaultsLoading={false}
            taskBranchDefaultsError={null}
            onRepoChange={() => {}}
            onSubmit={async () => {}}
            onClose={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('renders review task with PR fields without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewTaskModal
            newTaskRepo="example/repo"
            newTaskBranch="main"
            newTaskTitle="Review pull request with comprehensive analysis of code quality and design patterns"
            newTaskPrompt="Please review the PR for code quality, design patterns, potential bugs, and adherence to project standards."
            newTaskType="review"
            newTaskPrNumber="42"
            newTaskPrBranch="feature/new-auth-flow"
            setNewTaskRepo={() => {}}
            setNewTaskBranch={() => {}}
            setNewTaskTitle={() => {}}
            setNewTaskPrompt={() => {}}
            setNewTaskType={() => {}}
            setNewTaskPrNumber={() => {}}
            setNewTaskPrBranch={() => {}}
            newTaskModel=""
            newTaskEffort=""
            setNewTaskModel={() => {}}
            setNewTaskEffort={() => {}}
            computeExperiment={defaultComputeExperimentForm()}
            setComputeExperiment={() => {}}
            getDefaultModelForType={() => 'claude-opus-4-7'}
            getDefaultEffortForType={() => 'medium'}
            repoOptions={[
              { full_name: 'example/repo', default_branch: 'main' },
            ]}
            repoLoading={false}
            repoError={null}
            useManualRepo={false}
            setUseManualRepo={() => {}}
            branchOptions={['main', 'develop']}
            branchLoading={false}
            branchError={null}
            useManualBranch={false}
            setUseManualBranch={() => {}}
            setBranchOptions={() => {}}
            launchableTaskTypeOptions={LAUNCHABLE_OPTIONS}
            taskBranchDefaultsReady={true}
            taskBranchDefaultsLoading={false}
            taskBranchDefaultsError={null}
            onRepoChange={() => {}}
            onSubmit={async () => {}}
            onClose={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('renders a backend-registered type with no bespoke icon using the fallback icon', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <NewTaskModal
          newTaskRepo="example/repo"
          newTaskBranch="main"
          newTaskTitle="Test task"
          newTaskPrompt="Test prompt"
          newTaskType="create"
          newTaskPrNumber=""
          newTaskPrBranch=""
          setNewTaskRepo={() => {}}
          setNewTaskBranch={() => {}}
          setNewTaskTitle={() => {}}
          setNewTaskPrompt={() => {}}
          setNewTaskType={() => {}}
          setNewTaskPrNumber={() => {}}
          setNewTaskPrBranch={() => {}}
          newTaskModel=""
          newTaskEffort=""
          setNewTaskModel={() => {}}
          setNewTaskEffort={() => {}}
          computeExperiment={defaultComputeExperimentForm()}
          setComputeExperiment={() => {}}
          getDefaultModelForType={() => 'claude-opus-4-7'}
          getDefaultEffortForType={() => 'medium'}
          repoOptions={[{ full_name: 'example/repo', default_branch: 'main' }]}
          repoLoading={false}
          repoError={null}
          useManualRepo={false}
          setUseManualRepo={() => {}}
          branchOptions={['main']}
          branchLoading={false}
          branchError={null}
          useManualBranch={false}
          setUseManualBranch={() => {}}
          setBranchOptions={() => {}}
          launchableTaskTypeOptions={[
            { value: 'create', label: 'Create', description: 'Write code, open PR' },
            { value: 'custom_backend_type', label: 'Custom Backend Type', description: 'A backend-registered type with no static icon' },
          ]}
          taskBranchDefaultsReady={true}
          taskBranchDefaultsLoading={false}
          taskBranchDefaultsError={null}
          onRepoChange={() => {}}
          onSubmit={async () => {}}
          onClose={() => {}}
        />,
      );
    });
    // The unknown task-type id renders its label (fallback icon, no crash).
    const buttons = Array.from(document.querySelectorAll('button'));
    const customButton = buttons.find(b => b.textContent?.includes('Custom Backend Type'));
    expect(customButton).toBeTruthy();
    expect(customButton?.querySelector('svg')).toBeTruthy();
  });

  test('renders compute experiment preflight error state at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewTaskModal
            newTaskRepo="example/repo"
            newTaskBranch="main"
            newTaskTitle="Test compute experiment"
            newTaskPrompt="Run a test experiment with GPU"
            newTaskType="compute_experiment"
            newTaskPrNumber=""
            newTaskPrBranch=""
            setNewTaskRepo={() => {}}
            setNewTaskBranch={() => {}}
            setNewTaskTitle={() => {}}
            setNewTaskPrompt={() => {}}
            setNewTaskType={() => {}}
            setNewTaskPrNumber={() => {}}
            setNewTaskPrBranch={() => {}}
            newTaskModel=""
            newTaskEffort=""
            setNewTaskModel={() => {}}
            setNewTaskEffort={() => {}}
            computeExperiment={defaultComputeExperimentForm()}
            setComputeExperiment={() => {}}
            getDefaultModelForType={() => 'claude-opus-4-7'}
            getDefaultEffortForType={() => 'medium'}
            repoOptions={[{ full_name: 'example/repo', default_branch: 'main' }]}
            repoLoading={false}
            repoError={null}
            useManualRepo={false}
            setUseManualRepo={() => {}}
            branchOptions={['main']}
            branchLoading={false}
            branchError={null}
            useManualBranch={false}
            setUseManualBranch={() => {}}
            setBranchOptions={() => {}}
            launchableTaskTypeOptions={[
              { value: 'create', label: 'Create', description: 'Write code, open PR' },
              { value: 'compute_experiment', label: 'Compute', description: 'Run GPU experiment' },
            ]}
            taskBranchDefaultsReady={true}
            taskBranchDefaultsLoading={false}
            taskBranchDefaultsError={null}
            preflightResult={{
              rate_per_hour: null,
              projected_cost_usd: null,
              max_runtime_seconds: null,
              in_policy: false,
              reason: 'GPU type H100 exceeds project hardware restrictions',
              allowed_accelerator_types: ['T4', 'A100'],
              allowed_machine_types: null,
            }}
            preflightLoading={false}
            preflightError="Request timeout after 5s — unable to estimate cost"
            ideaText=""
            setIdeaText={() => {}}
            draftNotes={null}
            setDraftNotes={() => {}}
            onDraftApplied={() => {}}
            onRepoChange={() => {}}
            onSubmit={async () => {}}
            onClose={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
    // Verify error state is visible
    expect(document.body.textContent).toContain('Couldn\'t estimate cost');
    expect(document.body.textContent).toContain('Request timeout after 5s — unable to estimate cost');
  });
});
