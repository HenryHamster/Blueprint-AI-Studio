import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SystemMaturityCard } from './SystemMaturityCard';
import type { SystemMaturityRow } from './SystemMaturityCard';

function makeRow(overrides: Partial<SystemMaturityRow> = {}): SystemMaturityRow {
  return {
    system_id: 'sys-alpha',
    score: 75,
    recommended_mode: 'ship',
    top_issues: [],
    last_audited_at: '2026-04-28T00:00:00Z',
    ...overrides,
  };
}

describe('SystemMaturityCard', () => {
  it('renders skeleton while loading', () => {
    const { container } = render(<SystemMaturityCard rows={null} loading={true} />);
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });

  it('renders skeleton when rows is null but not loading', () => {
    const { container } = render(<SystemMaturityCard rows={null} loading={false} />);
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });

  it('renders empty state when rows array is empty', () => {
    render(<SystemMaturityCard rows={[]} loading={false} />);
    expect(screen.getByText('No audits have run yet')).toBeInTheDocument();
  });

  it('renders system row with system_id and score', () => {
    render(<SystemMaturityCard rows={[makeRow()]} loading={false} />);
    expect(screen.getByText('sys-alpha')).toBeInTheDocument();
    expect(screen.getByText('75')).toBeInTheDocument();
  });

  it('renders mode badge for rows with recommended_mode', () => {
    render(<SystemMaturityCard rows={[makeRow({ recommended_mode: 'audit' })]} loading={false} />);
    expect(screen.getByText('audit')).toBeInTheDocument();
  });

  it('does not render mode badge when recommended_mode is null', () => {
    render(<SystemMaturityCard rows={[makeRow({ recommended_mode: null })]} loading={false} />);
    expect(screen.queryByText('ship')).not.toBeInTheDocument();
  });

  it('score >= 70 uses emerald color class', () => {
    render(<SystemMaturityCard rows={[makeRow({ score: 80 })]} loading={false} />);
    const bar = document.querySelector('.bg-emerald-500');
    expect(bar).toBeInTheDocument();
  });

  it('score 40-69 uses amber color class', () => {
    render(<SystemMaturityCard rows={[makeRow({ score: 55 })]} loading={false} />);
    const bar = document.querySelector('.bg-amber-500');
    expect(bar).toBeInTheDocument();
  });

  it('score < 40 uses red color class', () => {
    render(<SystemMaturityCard rows={[makeRow({ score: 20 })]} loading={false} />);
    const bar = document.querySelector('.bg-red-500');
    expect(bar).toBeInTheDocument();
  });

  it('shows issue count button when top_issues is non-empty', () => {
    const issues = [{ description: 'Missing retries' }, { description: 'Stale cache' }];
    render(<SystemMaturityCard rows={[makeRow({ top_issues: issues })]} loading={false} />);
    expect(screen.getByRole('button', { name: /2 issues/ })).toBeInTheDocument();
  });

  it('expands issues on button click and collapses on second click', async () => {
    const user = userEvent.setup();
    const issues = [{ description: 'Missing retries' }];
    render(<SystemMaturityCard rows={[makeRow({ top_issues: issues })]} loading={false} />);

    expect(screen.queryByText('Missing retries')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /1 issues/ }));
    expect(screen.getByText('Missing retries')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'hide' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'hide' }));
    expect(screen.queryByText('Missing retries')).not.toBeInTheDocument();
  });

  it('renders multiple rows', () => {
    const rows = [
      makeRow({ system_id: 'sys-a', score: 90 }),
      makeRow({ system_id: 'sys-b', score: 30 }),
    ];
    render(<SystemMaturityCard rows={rows} loading={false} />);
    expect(screen.getByText('sys-a')).toBeInTheDocument();
    expect(screen.getByText('sys-b')).toBeInTheDocument();
  });

  it('falls back to JSON.stringify for issues without description field', async () => {
    const user = userEvent.setup();
    const issues = [{ severity: 'high', code: 'E001' }];
    render(<SystemMaturityCard rows={[makeRow({ top_issues: issues })]} loading={false} />);
    await user.click(screen.getByRole('button', { name: /1 issues/ }));
    expect(screen.getByText(JSON.stringify(issues[0]))).toBeInTheDocument();
  });
});
