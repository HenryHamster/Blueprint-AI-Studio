import { describe, test, expect, vi, afterEach } from 'vitest';
import { render, waitFor, act, fireEvent } from '@testing-library/react';
import type { ComputeJob } from '../api';
import { ComputeArtifactGallery } from './ComputeArtifactGallery';

const resolveArtifactDownloadUrl = vi.fn();

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    resolveArtifactDownloadUrl: (...args: unknown[]) => resolveArtifactDownloadUrl(...args),
  });
});

function makeJob(overrides: Partial<ComputeJob> = {}): ComputeJob {
  return {
    id: 'job-1',
    project_id: 'proj-1',
    task_id: 'task-1',
    backend: 'runner-subprocess',
    external_compute_id: null,
    machine_type: null,
    status: 'SUCCEEDED',
    terminal_reason: null,
    outcome: 'SUCCESS',
    spend_cap_usd: 5,
    wall_clock_deadline: null,
    created_at: '2026-07-11T10:00:00Z',
    updated_at: '2026-07-11T10:30:00Z',
    started_at: '2026-07-11T10:00:10Z',
    completed_at: '2026-07-11T10:29:50Z',
    last_polled_at: '2026-07-11T10:29:50Z',
    accrued_cost_usd: 0,
    final_cost_usd: 0,
    result_reference: null,
    accelerator_type: null,
    accelerator_count: null,
    ...overrides,
  } as ComputeJob;
}

const imageArtifact = {
  storage_uri: 'gs://bucket/plot.png',
  filename: 'plot.png',
  content_type: 'image/png',
  size_bytes: 2048,
  kind: 'image' as const,
};
const videoArtifact = {
  storage_uri: 'gs://bucket/rollout.mp4',
  filename: 'rollout.mp4',
  content_type: 'video/mp4',
  size_bytes: 5_000_000,
  kind: 'video' as const,
};

afterEach(() => {
  resolveArtifactDownloadUrl.mockReset();
  vi.restoreAllMocks();
});

describe('ComputeArtifactGallery', () => {
  test('renders nothing when the job has no viewable artifacts', () => {
    const { container } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: [] })} />,
    );
    expect(container.querySelector('[data-testid="drawer-artifact-gallery"]')).toBeNull();
  });

  test('renders nothing when projectId is empty', () => {
    const { container } = render(
      <ComputeArtifactGallery projectId="" job={makeJob({ viewable_artifacts: [imageArtifact] })} />,
    );
    expect(container.querySelector('[data-testid="drawer-artifact-gallery"]')).toBeNull();
  });

  test('shows a "Show previews (N)" toggle counting only valid artifacts', () => {
    const { getByTestId } = render(
      <ComputeArtifactGallery
        projectId="proj-1"
        // one malformed entry (missing storage_uri) must be dropped, not counted
        job={makeJob({ viewable_artifacts: [imageArtifact, videoArtifact, { kind: 'image' } as never] })}
      />,
    );
    expect(getByTestId('drawer-artifact-gallery-toggle').textContent).toBe('Show previews (2)');
  });

  test('expanding lazily resolves a signed URL once per artifact and renders image + video', async () => {
    resolveArtifactDownloadUrl.mockImplementation(async (_p: string, uri: string) => `https://signed/${uri}`);
    const { getByTestId, queryAllByTestId } = render(
      <ComputeArtifactGallery
        projectId="proj-1"
        job={makeJob({ viewable_artifacts: [imageArtifact, videoArtifact] })}
      />,
    );

    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });

    await waitFor(() => {
      expect(getByTestId('drawer-artifact-thumbnail')).toBeTruthy();
    });
    // One resolution call per artifact, each with (projectId, storage_uri).
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', imageArtifact.storage_uri);
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', videoArtifact.storage_uri);

    const img = getByTestId('drawer-artifact-thumbnail').querySelector('img');
    expect(img?.getAttribute('src')).toBe(`https://signed/${imageArtifact.storage_uri}`);
    const video = getByTestId('drawer-artifact-video');
    expect(video.getAttribute('src')).toBe(`https://signed/${videoArtifact.storage_uri}`);
    expect(queryAllByTestId('drawer-viewable-artifact')).toHaveLength(2);

    // Collapsing hides the previews.
    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    expect(queryAllByTestId('drawer-viewable-artifact')).toHaveLength(0);
  });

  test('a failed resolution shows a retry placeholder and a click re-resolves', async () => {
    resolveArtifactDownloadUrl.mockRejectedValueOnce(new Error('boom'));
    const { getByTestId } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: [imageArtifact] })} />,
    );

    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-placeholder').textContent).toContain('preview unavailable — retry');
    });

    resolveArtifactDownloadUrl.mockResolvedValueOnce('https://signed/retry.png');
    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-placeholder'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-thumbnail')).toBeTruthy();
    });
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
  });

  test('image onError flips a resolved thumbnail back to the retry placeholder', async () => {
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed/plot.png');
    const { getByTestId, queryByTestId } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: [imageArtifact] })} />,
    );

    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-thumbnail')).toBeTruthy();
    });

    const img = getByTestId('drawer-artifact-thumbnail').querySelector('img');
    expect(img).toBeTruthy();
    await act(async () => {
      fireEvent.error(img as HTMLImageElement);
    });
    expect(queryByTestId('drawer-artifact-thumbnail')).toBeNull();
    expect(getByTestId('drawer-artifact-placeholder').textContent).toContain('preview unavailable — retry');
  });
});
