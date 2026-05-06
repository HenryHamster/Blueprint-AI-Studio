import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ResearchBrowser } from './ResearchBrowser';
import type { Task, ResearchFinding } from '../types/task';

vi.mock('../api', () => ({
  API_BASE_URL: '',
  authHeaders: vi.fn(() => Promise.resolve({})),
}));

beforeEach(() => {
  // Stub fetch for the directions endpoint — return ok:false so it bails harmlessly.
  // Cast through unknown to satisfy the global fetch signature in test scope.
  globalThis.fetch = vi.fn(() => Promise.resolve({ ok: false, json: () => Promise.resolve({}) })) as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

function makeFinding(overrides: Partial<ResearchFinding> = {}): ResearchFinding {
  return {
    title: 'Finding A',
    description: 'evidence-bearing finding',
    prompt: 'do X',
    scope: 'small',
    risk: 'low',
    priority: 'medium',
    related_files: [],
    ...overrides,
  };
}

function makeResearchTask(findings: ResearchFinding[]): Task {
  return {
    id: 'research-1',
    title: 'Research task',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'research',
    created_at: '2026-05-01T00:00:00Z',
    created_by: 'AGENT',
    requires_approval: false,
    research_findings: findings,
  };
}

describe('ResearchBrowser evidence panel', () => {
  it('renders claims with show-more toggle and source refs', async () => {
    const user = userEvent.setup();
    const finding = makeFinding({
      claims: ['claim a', 'claim b', 'claim c', 'claim d'],
      sources: [
        { type: 'file', ref: 'agent/src/foo.ts:42' },
        { type: 'pr', ref: 'pr#3874' },
        { type: 'log', ref: 'https://example.com/log' },
      ],
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([finding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByText('Finding A'));

    // First 3 claims visible; the 4th hidden behind toggle.
    expect(screen.getByText('claim a')).toBeInTheDocument();
    expect(screen.getByText('claim b')).toBeInTheDocument();
    expect(screen.getByText('claim c')).toBeInTheDocument();
    expect(screen.queryByText('claim d')).not.toBeInTheDocument();

    await user.click(screen.getByText(/^Show 1 more$/));
    expect(screen.getByText('claim d')).toBeInTheDocument();

    // External https URL renders as anchor with target=_blank.
    const link = screen.getByRole('link', { name: 'https://example.com/log' });
    expect(link).toHaveAttribute('href', 'https://example.com/log');
    expect(link).toHaveAttribute('target', '_blank');

    // pr#3874 has no repo_url to pin against — renders as monospaced text.
    expect(screen.getByText('pr#3874')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'pr#3874' })).toBeNull();

    // File path also has no repo_url — renders as text.
    expect(screen.getByText('agent/src/foo.ts:42')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'agent/src/foo.ts:42' })).toBeNull();
  });

  it('renders nothing when finding has no claims and no sources', async () => {
    const user = userEvent.setup();
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Plain finding' })])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByText('Plain finding'));
    expect(screen.queryByText(/^Claims$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Sources$/i)).not.toBeInTheDocument();
  });
});
