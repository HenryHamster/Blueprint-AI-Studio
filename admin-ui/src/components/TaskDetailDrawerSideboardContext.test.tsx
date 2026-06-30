import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test, vi } from 'vitest';
import type { Task } from '../types/task';
import { TaskSideboardContextSection } from './TaskDetailDrawerSideboardContext';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-12345678',
    project_id: 'proj-1',
    title: 'Implement sideboard polish',
    description: 'Task description',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'ORCHESTRATOR',
    requires_approval: false,
    created_at: '2026-06-29T12:00:00Z',
    sideboard_context: {
      project: { id: 'proj-1', name: 'Blueprint', repo: 'HenryHamster/BlueprintAIStudio' },
      purpose: 'Make passive work readable from the task drawer.',
      strategic: {
        direction: {
          id: 'direction-long-running-work',
          text: 'Keep monitoring context dense and legible for passive tasks.',
          priority: 'high',
          status: 'active',
        },
        cycle: {
          id: 'cycle-2026-06-30',
          phase: 'implementation_review',
          outcome: 'accepted',
        },
      },
      pipeline: {
        parent_task_id: 'parent-task-abcdef',
        child_count: 3,
        dependency_count: 2,
        blocked_reason: 'Waiting for upstream verification.',
      },
      artifacts: {
        total: 2,
        by_type: { PULL_REQUEST: 1, LOG: 1 },
        primary_pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
        primary_commit: 'abcdef1234567890',
      },
      messages: {
        total_recent: 3,
        items: [
          { id: 'message-1', role: 'assistant', content: 'Validated the compact sideboard rows.' },
          { id: 'message-2', role: 'user', content: 'Keep all existing task context visible.' },
        ],
      },
      links: [
        {
          id: 'link-task',
          label: 'Open child task',
          target: { surface: 'task', id: 'child-task-123456', task_id: 'child-task-123456' },
        },
        {
          id: 'link-external',
          label: 'Review evidence',
          target: { surface: 'external', id: 'https://example.com/evidence', url: 'https://example.com/evidence' },
        },
      ],
    },
    ...overrides,
  };
}

describe('TaskSideboardContextSection', () => {
  test('groups sideboard context without dropping passive work details', async () => {
    const user = userEvent.setup();
    const onNavigateToTask = vi.fn();
    render(<TaskSideboardContextSection task={makeTask()} onNavigateToTask={onNavigateToTask} />);

    expect(screen.getByText('Context')).toBeInTheDocument();
    expect(screen.getByText('HenryHamster/BlueprintAIStudio')).toBeInTheDocument();
    expect(screen.getByText('Make passive work readable from the task drawer.')).toBeInTheDocument();
    expect(screen.getByText('Keep monitoring context dense and legible for passive tasks.')).toBeInTheDocument();
    expect(screen.getByText('cycle-2026-06-30')).toBeInTheDocument();
    expect(screen.getByText('implementation review')).toBeInTheDocument();
    expect(screen.getByText('Dependencies')).toBeInTheDocument();
    expect(screen.getByText('Children')).toBeInTheDocument();
    expect(screen.getByText('Waiting for upstream verification.')).toBeInTheDocument();
    expect(screen.getByText('pull request 1 · log 1')).toBeInTheDocument();
    expect(screen.getByText('abcdef123456')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'PR' })).toHaveAttribute(
      'href',
      'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
    );
    expect(screen.getByText('Open child task')).toBeInTheDocument();
    expect(screen.getByText('Review evidence')).toBeInTheDocument();
    expect(screen.getByText('Validated the compact sideboard rows.')).toBeInTheDocument();
    expect(screen.getByText('+1 more recent message')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Open child task/i }));
    expect(onNavigateToTask).toHaveBeenCalledWith('child-task-123456');

    await user.click(screen.getByRole('button', { name: 'parent parent-task-abcdef' }));
    expect(onNavigateToTask).toHaveBeenCalledWith('parent-task-abcdef');
  });
});
