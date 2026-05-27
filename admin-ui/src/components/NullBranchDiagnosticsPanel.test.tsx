import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';
import { NullBranchDiagnosticsPanel } from './NullBranchDiagnosticsPanel';

const emptyData = { projects: [], self_repo_skipped: [], total: 0 };

const populatedData = {
  projects: [
    { id: 'p1', name: 'Acme API', github_owner: 'acme', github_repo: 'api', is_self_repo: false, created_at: null },
    { id: 'p2', name: 'Acme Frontend', github_owner: 'acme', github_repo: 'frontend', is_self_repo: false, created_at: null },
  ],
  self_repo_skipped: [
    { id: 'p3', name: 'Blueprint Self', github_owner: 'blueprint', github_repo: 'self', is_self_repo: true, created_at: null },
  ],
  total: 3,
};

vi.mock('../api', () => ({
  fetchNullBranchConfig: vi.fn(),
  backfillProjectBranches: vi.fn(),
  apiFetch: vi.fn(),
  authGet: vi.fn(),
  API_BASE_URL: 'http://localhost:8000',
}));

import { fetchNullBranchConfig, backfillProjectBranches } from '../api';

describe('NullBranchDiagnosticsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  test('renders nothing when projects is empty', async () => {
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(emptyData);
    const { container } = render(<NullBranchDiagnosticsPanel />);
    await waitFor(() => {
      // After loading, no content rendered for empty state
      expect(container.firstChild).toBeNull();
    });
  });

  test('renders project rows with github repo slug and Backfill button', async () => {
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(populatedData);
    render(<NullBranchDiagnosticsPanel />);

    await screen.findByText('Acme API');
    expect(screen.getByText('acme/api')).toBeTruthy();
    expect(screen.getByText('Acme Frontend')).toBeTruthy();
    expect(screen.getByText('acme/frontend')).toBeTruthy();
    expect(screen.getAllByText('Backfill')).toHaveLength(2);
  });

  test('shows 2 need fix badge when projects present', async () => {
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(populatedData);
    render(<NullBranchDiagnosticsPanel />);

    await screen.findByText('2 need fix');
  });

  test('self_repo_skipped section is hidden behind disclosure by default', async () => {
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(populatedData);
    render(<NullBranchDiagnosticsPanel />);

    await screen.findByText('Acme API');
    // Self-repo content not visible by default
    expect(screen.queryByText('Blueprint Self')).toBeNull();
    expect(screen.getByText(/1 self-repo skipped/)).toBeTruthy();
  });

  test('self_repo_skipped section expands when disclosure clicked', async () => {
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(populatedData);
    render(<NullBranchDiagnosticsPanel />);

    await screen.findByText('Acme API');
    const disclosure = screen.getByText(/1 self-repo skipped/);
    fireEvent.click(disclosure);

    expect(screen.getByText('Blueprint Self')).toBeTruthy();
    expect(screen.getByText('blueprint/self')).toBeTruthy();
  });

  test('Backfill button calls backfillProjectBranches and removes row on success', async () => {
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(populatedData);
    (backfillProjectBranches as ReturnType<typeof vi.fn>).mockResolvedValue({});
    render(<NullBranchDiagnosticsPanel />);

    await screen.findByText('Acme API');
    const buttons = screen.getAllByText('Backfill');
    fireEvent.click(buttons[0]);

    await waitFor(() => {
      expect(backfillProjectBranches).toHaveBeenCalledWith('p1');
      expect(screen.queryByText('Acme API')).toBeNull();
    });
    // Second project still present
    expect(screen.getByText('Acme Frontend')).toBeTruthy();
  });

  test('shows error message when Backfill fails', async () => {
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(populatedData);
    (backfillProjectBranches as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Network error'));
    render(<NullBranchDiagnosticsPanel />);

    await screen.findByText('Acme API');
    const buttons = screen.getAllByText('Backfill');
    fireEvent.click(buttons[0]);

    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeTruthy();
    });
    // Row stays visible on error
    expect(screen.getByText('Acme API')).toBeTruthy();
  });

  test('only self_repo_skipped — panel still renders the disclosure', async () => {
    const onlySelf = { projects: [], self_repo_skipped: populatedData.self_repo_skipped, total: 1 };
    (fetchNullBranchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(onlySelf);
    render(<NullBranchDiagnosticsPanel />);

    await screen.findByText(/1 self-repo skipped/);
    expect(screen.queryByText('need fix')).toBeNull();
  });
});
