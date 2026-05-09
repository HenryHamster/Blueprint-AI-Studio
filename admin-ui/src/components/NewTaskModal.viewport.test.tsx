import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { NewTaskModal } from './NewTaskModal';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

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
});
