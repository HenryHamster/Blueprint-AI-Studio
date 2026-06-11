import { afterEach, describe, expect, test, vi } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import { PipelineRegressionsPanel } from './PipelineRegressionsPanel';
import { formatAbsolute, parseTimestamp } from '../utils/time';

const STORAGE_KEY = 'blueprint-admin:pipelineRegressionsPanelCollapsed';

const fetchPipelineRegressions = vi.fn();

vi.mock('../api', () => ({
  fetchPipelineRegressions: (...args: unknown[]) => fetchPipelineRegressions(...args),
}));

describe('PipelineRegressionsPanel', () => {
  afterEach(() => {
    cleanup();
    fetchPipelineRegressions.mockReset();
    localStorage.removeItem(STORAGE_KEY);
  });

  test('renders a healthy empty state when the backend returns no active regressions', async () => {
    fetchPipelineRegressions.mockResolvedValueOnce({
      project_id: 'proj-1',
      regressions: [],
      healthy: true,
    });

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    expect(await screen.findByText('Healthy')).toBeTruthy();
    expect(screen.getByText('No active pipeline regressions')).toBeTruthy();
    expect(screen.queryAllByTestId('pipeline-regression-row')).toHaveLength(0);
  });

  test('renders active regressions with severity, detected-at, and rate change', async () => {
    fetchPipelineRegressions.mockResolvedValueOnce({
      project_id: 'proj-1',
      regressions: [
        {
          signal_name: 'SR catch-rate regression: create',
          regression_class: 'sr_catch_rate:create',
          detected_at: '2026-06-08T10:00:00',
          severity: 'warning',
          current_rate: 0.42,
          prior_rate: 0.31,
        },
        {
          signal_name: 'Background service crashed: digest worker',
          regression_class: 'background_service_crash:digest-worker',
          detected_at: '2026-06-08T11:15:00',
          severity: 'critical',
          current_rate: null,
          prior_rate: null,
        },
      ],
      healthy: false,
    });

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('pipeline-regression-row');
    expect(rows).toHaveLength(2);
    expect(screen.getByText('SR catch-rate regression: create')).toBeTruthy();
    expect(screen.getByText('Background service crashed: digest worker')).toBeTruthy();
    expect(screen.getByText('warning')).toBeTruthy();
    expect(screen.getByText('critical')).toBeTruthy();
    expect(screen.getByText('sr_catch_rate:create')).toBeTruthy();
    expect(screen.getByText('background_service_crash:digest-worker')).toBeTruthy();
    expect(screen.getByText('+11.0 pts')).toBeTruthy();
    // Mirror the component's formatting: rendered detected-at is local time,
    // so a raw '10:00:00' substring match only passes when local TZ == UTC.
    const expectedDetectedAt = formatAbsolute(parseTimestamp('2026-06-08T10:00:00').getTime());
    expect(screen.getByText((text) => text.includes(expectedDetectedAt))).toBeTruthy();
  });

  test('renders an error state when the fetch fails', async () => {
    fetchPipelineRegressions.mockRejectedValueOnce(new Error('boom'));

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    expect(await screen.findByText('boom')).toBeTruthy();
    await waitFor(() => expect(screen.queryByText('No active pipeline regressions')).toBeNull());
  });

  test('collapses the body on header click, keeps the badge, and persists the choice', async () => {
    fetchPipelineRegressions.mockResolvedValue({
      project_id: 'proj-1',
      regressions: [
        {
          signal_name: 'SR catch-rate regression: create',
          regression_class: 'sr_catch_rate:create',
          detected_at: '2026-06-08T10:00:00',
          severity: 'warning',
          current_rate: 0.42,
          prior_rate: 0.31,
        },
      ],
      healthy: false,
    });

    render(<PipelineRegressionsPanel projectId="proj-1" />);
    expect(await screen.findAllByTestId('pipeline-regression-row')).toHaveLength(1);

    fireEvent.click(screen.getByTestId('pipeline-regressions-toggle'));

    expect(screen.queryAllByTestId('pipeline-regression-row')).toHaveLength(0);
    expect(screen.getByText('1 active')).toBeTruthy();
    expect(localStorage.getItem(STORAGE_KEY)).toBe('true');

    fireEvent.click(screen.getByTestId('pipeline-regressions-toggle'));
    expect(await screen.findAllByTestId('pipeline-regression-row')).toHaveLength(1);
    expect(localStorage.getItem(STORAGE_KEY)).toBe('false');
  });

  test('starts collapsed when the stored preference says so', async () => {
    localStorage.setItem(STORAGE_KEY, 'true');
    fetchPipelineRegressions.mockResolvedValue({
      project_id: 'proj-1',
      regressions: [],
      healthy: true,
    });

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    expect(await screen.findByText('Healthy')).toBeTruthy();
    expect(screen.queryByText('No active pipeline regressions')).toBeNull();
  });
});
