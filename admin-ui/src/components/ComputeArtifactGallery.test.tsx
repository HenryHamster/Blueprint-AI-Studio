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
const pdfArtifact = {
  storage_uri: 'gs://bucket/report.pdf',
  filename: 'report.pdf',
  content_type: 'application/pdf',
  size_bytes: 100_000,
  kind: 'pdf' as const,
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

  test('renders the omitted-file note (no toggle) when every viewable file was capped away', () => {
    const { getByTestId, queryByTestId } = render(
      <ComputeArtifactGallery
        projectId="proj-1"
        job={makeJob({ viewable_artifacts: [], viewable_artifacts_dropped_count: 3 })}
      />,
    );
    expect(getByTestId('drawer-artifact-gallery')).toBeTruthy();
    expect(getByTestId('drawer-artifact-omitted-note').textContent).toBe(
      '3 files omitted (exceeds preview cap: 25 files / 8 MiB each)',
    );
    // No previews to toggle when everything was dropped.
    expect(queryByTestId('drawer-artifact-gallery-toggle')).toBeNull();
  });

  test('uses singular copy in the omitted note for a single dropped file', () => {
    const { getByTestId } = render(
      <ComputeArtifactGallery
        projectId="proj-1"
        job={makeJob({ viewable_artifacts: [], viewable_artifacts_dropped_count: 1 })}
      />,
    );
    expect(getByTestId('drawer-artifact-omitted-note').textContent).toBe(
      '1 file omitted (exceeds preview cap: 25 files / 8 MiB each)',
    );
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

  test('a pdf artifact renders an Open-PDF button (never an <img>) and opens a new tab on click', async () => {
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed/report.pdf');
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    const { getByTestId, queryByTestId } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: [pdfArtifact] })} />,
    );

    // Expanding must NOT eagerly sign the pdf's URL (click-to-open only).
    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    expect(resolveArtifactDownloadUrl).not.toHaveBeenCalled();

    const pdfButton = getByTestId('drawer-artifact-pdf');
    expect(pdfButton.textContent).toContain('Open PDF');
    // A pdf must never fall through to the image branch.
    expect(queryByTestId('drawer-artifact-thumbnail')).toBeNull();
    expect(pdfButton.querySelector('img')).toBeNull();

    await act(async () => {
      fireEvent.click(pdfButton);
    });
    await waitFor(() => {
      expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', pdfArtifact.storage_uri);
    });
    expect(openSpy).toHaveBeenCalled();
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

  test('video onError flips a resolved video back to the retry placeholder', async () => {
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed/rollout.mp4');
    const { getByTestId, queryByTestId } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: [videoArtifact] })} />,
    );

    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-video')).toBeTruthy();
    });

    const video = getByTestId('drawer-artifact-video') as HTMLVideoElement;
    await act(async () => {
      fireEvent.error(video);
    });
    expect(queryByTestId('drawer-artifact-video')).toBeNull();
    expect(getByTestId('drawer-artifact-placeholder').textContent).toContain('preview unavailable — retry');
  });

  test('pdf open failure shows "open failed — retry" and a second click re-resolves', async () => {
    resolveArtifactDownloadUrl.mockRejectedValueOnce(new Error('storage unavailable'));
    vi.spyOn(window, 'open').mockReturnValue(null);

    const { getByTestId } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: [pdfArtifact] })} />,
    );

    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });

    const pdfButton = getByTestId('drawer-artifact-pdf');
    expect(pdfButton.textContent).toContain('Open PDF');

    // First click fails — shows error state.
    await act(async () => {
      fireEvent.click(pdfButton);
    });
    await waitFor(() => {
      expect(pdfButton.textContent).toContain('open failed — retry');
    });

    // Second click succeeds.
    resolveArtifactDownloadUrl.mockResolvedValueOnce('https://signed/report.pdf');
    await act(async () => {
      fireEvent.click(pdfButton);
    });
    await waitFor(() => {
      expect(pdfButton.textContent).toContain('Open PDF');
    });
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
  });

  test('opening an image full-size when the cached URL is stale triggers re-resolution', async () => {
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed/plot-v1.png');
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);

    const { getByTestId } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ viewable_artifacts: [imageArtifact] })} />,
    );

    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-thumbnail')).toBeTruthy();
    });
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(1);

    // Simulate the cached URL going stale by advancing system time.
    const FRESH_MS = 50 * 60 * 1000; // mirrors SIGNED_PREVIEW_FRESH_MS in the component
    vi.setSystemTime(Date.now() + FRESH_MS + 1000);

    resolveArtifactDownloadUrl.mockResolvedValue('https://signed/plot-v2.png');
    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-thumbnail'));
    });
    await waitFor(() => {
      expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
    });
    expect(openSpy).toHaveBeenCalled();

    vi.useRealTimers();
  });

  test('refreshes an open same-URI preview by revision and reconciles live to terminal', async () => {
    resolveArtifactDownloadUrl
      .mockResolvedValueOnce('https://signed/plot-v1.png')
      .mockResolvedValueOnce('https://signed/plot-v2.png');
    const live = { ...imageArtifact, revision: 'v1', live: true };
    const { getByTestId, rerender } = render(
      <ComputeArtifactGallery projectId="proj-1" job={makeJob({ status: 'RUNNING', viewable_artifacts: [live] })} />,
    );

    fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    await waitFor(() => expect(getByTestId('drawer-artifact-thumbnail').querySelector('img')?.src)
      .toBe('https://signed/plot-v1.png'));

    rerender(
      <ComputeArtifactGallery
        projectId="proj-1"
        job={makeJob({
          status: 'SUCCEEDED',
          viewable_artifacts: [{ ...live, revision: 'v2', live: false }],
        })}
      />,
    );

    expect(getByTestId('drawer-artifact-gallery-toggle').textContent).toBe('Hide previews');
    await waitFor(() => expect(getByTestId('drawer-artifact-thumbnail').querySelector('img')?.src)
      .toBe('https://signed/plot-v2.png'));
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
  });

  test('expanding gallery with image, video, and pdf resolves image+video but never pdf eagerly', async () => {
    resolveArtifactDownloadUrl.mockImplementation(async (_p: string, uri: string) => `https://signed/${uri}`);
    const { getByTestId, queryAllByTestId } = render(
      <ComputeArtifactGallery
        projectId="proj-1"
        job={makeJob({ viewable_artifacts: [imageArtifact, videoArtifact, pdfArtifact] })}
      />,
    );

    expect(getByTestId('drawer-artifact-gallery-toggle').textContent).toBe('Show previews (3)');

    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-thumbnail')).toBeTruthy();
      expect(getByTestId('drawer-artifact-video')).toBeTruthy();
    });

    // PDF is never eagerly resolved — no signed URL call for it.
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', imageArtifact.storage_uri);
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', videoArtifact.storage_uri);
    expect(resolveArtifactDownloadUrl).not.toHaveBeenCalledWith('proj-1', pdfArtifact.storage_uri);

    // All three artifact entries are rendered.
    expect(queryAllByTestId('drawer-viewable-artifact')).toHaveLength(3);
    // PDF button shows its standard label, not an error.
    expect(getByTestId('drawer-artifact-pdf').textContent).toContain('Open PDF');
  });

  test('re-expanding a gallery after collapse does not re-resolve already-resolved URLs', async () => {
    resolveArtifactDownloadUrl.mockImplementation(async (_p: string, uri: string) => `https://signed/${uri}`);
    const { getByTestId } = render(
      <ComputeArtifactGallery
        projectId="proj-1"
        job={makeJob({ viewable_artifacts: [imageArtifact, videoArtifact] })}
      />,
    );

    // First expand: resolves both.
    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-thumbnail')).toBeTruthy();
    });
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);

    // Collapse.
    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });

    // Re-expand: cached URLs are still fresh, no additional resolution calls.
    await act(async () => {
      fireEvent.click(getByTestId('drawer-artifact-gallery-toggle'));
    });
    await waitFor(() => {
      expect(getByTestId('drawer-artifact-thumbnail')).toBeTruthy();
    });
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
  });
});
