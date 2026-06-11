import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/react';
import { PipelineRegressionsPanel } from './PipelineRegressionsPanel';
import { formatAbsolute, parseTimestamp } from '../utils/time';

const STORAGE_KEY = 'blueprint-admin:pipelineRegressionsPanelCollapsed';
const LEGACY_STORAGE_KEY = 'blueprint-admin:pipelineRegressionsCollapsed';

const fetchPipelineRegressions = vi.fn();
const resolveViewportFailureDownloadUrl = vi.fn();

vi.mock('../api', () => ({
  fetchPipelineRegressions: (...args: unknown[]) => fetchPipelineRegressions(...args),
  resolveViewportFailureDownloadUrl: (...args: unknown[]) => resolveViewportFailureDownloadUrl(...args),
}));

const ACTIVE_REGRESSIONS = {
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
};

describe('PipelineRegressionsPanel', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    fetchPipelineRegressions.mockReset();
    resolveViewportFailureDownloadUrl.mockReset();
    localStorage.clear();
  });

  test('defaults to collapsed when healthy but keeps the Healthy badge visible', async () => {
    fetchPipelineRegressions.mockResolvedValueOnce({
      project_id: 'proj-1',
      regressions: [],
      healthy: true,
    });

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    // Header badge stays visible even though the body is collapsed.
    expect(await screen.findByText('Healthy')).toBeTruthy();
    expect(screen.getByRole('button', { name: /pipeline regressions/i }).getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('No active pipeline regressions')).toBeNull();
  });

  test('expands the healthy body when the header is toggled', async () => {
    fetchPipelineRegressions.mockResolvedValue({
      project_id: 'proj-1',
      regressions: [],
      healthy: true,
    });

    render(<PipelineRegressionsPanel projectId="proj-1" />);
    await screen.findByText('Healthy');

    fireEvent.click(screen.getByRole('button', { name: /pipeline regressions/i }));

    expect(screen.getByText('No active pipeline regressions')).toBeTruthy();
    expect(localStorage.getItem(STORAGE_KEY)).toBe('false');
  });

  test('renders active regressions expanded by default with compact rows', async () => {
    fetchPipelineRegressions.mockResolvedValueOnce(ACTIVE_REGRESSIONS);

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('pipeline-regression-row');
    expect(rows).toHaveLength(2);
    expect(screen.getByText('SR catch-rate regression: create')).toBeTruthy();
    expect(screen.getByText('Background service crashed: digest worker')).toBeTruthy();
    expect(screen.getByText('warning')).toBeTruthy();
    expect(screen.getByText('critical')).toBeTruthy();
    // regression_class (the dedup key) stays visible in the compact row.
    expect(screen.getByText('sr_catch_rate:create')).toBeTruthy();
    expect(screen.getByText('background_service_crash:digest-worker')).toBeTruthy();
    expect(screen.getByText('+11.0 pts')).toBeTruthy();
    // Mirror the component's formatting: rendered detected-at is local time,
    // so a raw '10:00:00' substring match only passes when local TZ == UTC.
    const expectedDetectedAt = formatAbsolute(parseTimestamp('2026-06-08T10:00:00').getTime());
    expect(screen.getByText((text) => text.includes(expectedDetectedAt))).toBeTruthy();
    // "N active" badge is in the header.
    expect(screen.getByText('2 active')).toBeTruthy();
  });

  test('expands a row to reveal current / prior detail', async () => {
    fetchPipelineRegressions.mockResolvedValueOnce(ACTIVE_REGRESSIONS);

    render(<PipelineRegressionsPanel projectId="proj-1" />);
    const rows = await screen.findAllByTestId('pipeline-regression-row');

    // Detail labels not shown until a row is expanded.
    expect(screen.queryByText('Current')).toBeNull();

    fireEvent.click(within(rows[0]).getByRole('button'));

    expect(screen.getByText('Current')).toBeTruthy();
    expect(screen.getByText('Prior / change')).toBeTruthy();
    expect(screen.getByText('42.0%')).toBeTruthy();
    expect(screen.getByText('31.0%')).toBeTruthy();
  });

  test('renders viewport regression screenshot and visible unavailable state', async () => {
    fetchPipelineRegressions.mockResolvedValueOnce({
      project_id: 'proj-1',
      healthy: false,
      regressions: [
        {
          signal_name: 'Viewport regression: MessagesPanel at 375px',
          regression_class: 'viewport_regression:MessagesPanel:overflow@375',
          detected_at: '2026-06-08T10:00:00',
          severity: 'critical',
          current_rate: null,
          prior_rate: null,
          viewport_component: 'MessagesPanel',
          viewport_assertion_type: 'overflow',
          viewport_width: 375,
        },
        {
          signal_name: 'Viewport regression without metadata',
          regression_class: 'viewport_regression:unknown',
          detected_at: '2026-06-08T10:05:00',
          severity: 'warning',
          current_rate: null,
          prior_rate: null,
        },
      ],
    });
    resolveViewportFailureDownloadUrl.mockResolvedValueOnce('https://storage.example/MessagesPanel-375.png');

    render(<PipelineRegressionsPanel projectId="proj-1" />);
    const rows = await screen.findAllByTestId('pipeline-regression-row');

    fireEvent.click(within(rows[0]).getByRole('button'));
    const image = await screen.findByAltText('MessagesPanel viewport failure at 375px');
    expect(image.getAttribute('src')).toBe('https://storage.example/MessagesPanel-375.png');
    expect(resolveViewportFailureDownloadUrl).toHaveBeenCalledWith(
      'proj-1',
      'MessagesPanel',
      375,
      expect.any(AbortSignal),
    );

    fireEvent.click(within(rows[1]).getByRole('button'));
    expect(screen.getByText('Screenshot unavailable: missing viewport metadata')).toBeTruthy();
  });

  test('shows unavailable state for viewport resolver rejection and image load failure', async () => {
    fetchPipelineRegressions.mockResolvedValueOnce({
      project_id: 'proj-1',
      healthy: false,
      regressions: [
        {
          signal_name: 'Viewport regression: MessagesPanel at 375px',
          regression_class: 'viewport_regression:MessagesPanel:overflow@375',
          detected_at: '2026-06-08T10:00:00',
          severity: 'critical',
          current_rate: null,
          prior_rate: null,
          viewport_component: 'MessagesPanel',
          viewport_assertion_type: 'overflow',
          viewport_width: 375,
        },
        {
          signal_name: 'Viewport regression: TaskCard at 768px',
          regression_class: 'viewport_regression:TaskCard:touch_target@768',
          detected_at: '2026-06-08T10:05:00',
          severity: 'warning',
          current_rate: null,
          prior_rate: null,
          viewport_component: 'TaskCard',
          viewport_assertion_type: 'touch_target',
          viewport_width: 768,
        },
      ],
    });
    resolveViewportFailureDownloadUrl
      .mockRejectedValueOnce(new Error('Artifact storage unavailable'))
      .mockResolvedValueOnce('https://storage.example/TaskCard-768.png');

    render(<PipelineRegressionsPanel projectId="proj-1" />);
    const rows = await screen.findAllByTestId('pipeline-regression-row');

    fireEvent.click(within(rows[0]).getByRole('button'));
    expect(await screen.findByText('Screenshot unavailable: Artifact storage unavailable')).toBeTruthy();

    fireEvent.click(within(rows[1]).getByRole('button'));
    const image = await screen.findByAltText('TaskCard viewport failure at 768px');
    fireEvent.error(image);

    expect(screen.getByText('Screenshot unavailable: image failed to load')).toBeTruthy();
  });

  test('shows worst active severity in the header when collapsed', async () => {
    // Operator has explicitly collapsed the panel previously.
    localStorage.setItem(LEGACY_STORAGE_KEY, 'true');
    fetchPipelineRegressions.mockResolvedValueOnce(ACTIVE_REGRESSIONS);

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    // Body is collapsed: no rows rendered.
    await waitFor(() => expect(screen.getByText('2 active')).toBeTruthy());
    expect(screen.queryAllByTestId('pipeline-regression-row')).toHaveLength(0);
    // Worst severity (critical beats warning) surfaced in the collapsed header.
    expect(screen.getByTestId('pipeline-regression-worst-severity').textContent).toBe('critical');
  });

  test('persists the collapsed preference across remounts', async () => {
    fetchPipelineRegressions.mockResolvedValue(ACTIVE_REGRESSIONS);

    const { unmount } = render(<PipelineRegressionsPanel projectId="proj-1" />);
    await screen.findAllByTestId('pipeline-regression-row');

    // Collapse via header toggle.
    fireEvent.click(screen.getByRole('button', { name: /pipeline regressions/i }));
    expect(localStorage.getItem(STORAGE_KEY)).toBe('true');
    unmount();

    // Remount reads the persisted preference and starts collapsed.
    render(<PipelineRegressionsPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('2 active')).toBeTruthy());
    expect(screen.queryAllByTestId('pipeline-regression-row')).toHaveLength(0);
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

  test('reads the legacy automain collapse preference key', async () => {
    localStorage.setItem(LEGACY_STORAGE_KEY, 'true');
    fetchPipelineRegressions.mockResolvedValueOnce(ACTIVE_REGRESSIONS);

    render(<PipelineRegressionsPanel projectId="proj-1" />);

    await waitFor(() => expect(screen.getByText('2 active')).toBeTruthy());
    expect(screen.queryAllByTestId('pipeline-regression-row')).toHaveLength(0);
  });
});
