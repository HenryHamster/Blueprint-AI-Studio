import { afterEach, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ArtifactViewablePreviews, StrategicContextArtifactSlot } from './StrategicContextArtifactSlot';
import type {
  StrategicContextArtifactViewablePreview,
  StrategicContextPackageDescriptor,
  StrategicContextRenderableDescriptor,
} from '../strategicStateApi';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    resolveArtifactDownloadUrl: vi.fn((_projectId: string, storageUri: string) =>
      Promise.resolve(`https://fresh.example.com/signed?ref=${encodeURIComponent(storageUri)}`),
    ),
  });
});

function makePackage(overrides: Partial<StrategicContextPackageDescriptor> = {}): StrategicContextPackageDescriptor {
  return {
    id: 'pkg-test',
    title: 'Training run results',
    summary: 'Compute experiment completed.',
    status: 'ready',
    descriptors: [],
    ...overrides,
  };
}

describe('StrategicContextArtifactSlot — lazy signed-URL resolution', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    const r = root;
    if (r) { act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function render(pkg: StrategicContextPackageDescriptor, projectId?: string | null) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<StrategicContextArtifactSlot contextPackage={pkg} projectId={projectId} />);
    });
    // Let the resolver promise settle and re-render with the fresh URL.
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
  }

  test('re-resolves a fresh signed URL from storage_uri when projectId is present', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-image',
          kind: 'image',
          title: 'Reward plot',
          status: 'ready',
          image: {
            url: 'https://storage.example.com/plot.png?sig=stale',
            alt: 'Reward plot',
            storage_uri: 'gs://bucket/proj/plot.png',
          },
          images: [
            {
              url: 'https://storage.example.com/plot.png?sig=stale',
              alt: 'plot.png',
              filename: 'plot.png',
              storage_uri: 'gs://bucket/proj/plot.png',
            },
          ],
        },
      ],
    });

    await render(pkg, 'proj-1');

    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledWith(
      'proj-1',
      'gs://bucket/proj/plot.png',
      expect.anything(),
    );
    const img = host!.querySelector('img');
    expect(img).not.toBeNull();
    // The rendered src is the freshly minted URL, not the baked (stale) one.
    expect(img!.getAttribute('src')).toContain('fresh.example.com');
    expect(img!.getAttribute('src')).not.toContain('sig=stale');
  });

  test('refreshes a mounted same-URI canvas preview when its revision changes', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    vi.mocked(resolveArtifactDownloadUrl)
      .mockResolvedValueOnce('https://fresh.example.com/plot-v1.png')
      .mockResolvedValueOnce('https://fresh.example.com/plot-v2.png');
    const descriptor = imageDescriptor({
      image: { url: 'https://baked.example/plot-v1.png', alt: 'Reward plot', storage_uri: 'gs://bucket/proj/plot.png', revision: 'v1', live: true },
      images: [{ url: 'https://baked.example/plot-v1.png', alt: 'plot.png', filename: 'plot.png', storage_uri: 'gs://bucket/proj/plot.png', revision: 'v1', live: true }],
    });
    await render(makePackage({ descriptors: [descriptor] }), 'proj-1');
    expect(host!.querySelector('img')?.getAttribute('src')).toBe('https://fresh.example.com/plot-v1.png');

    await act(async () => {
      root!.render(<StrategicContextArtifactSlot contextPackage={makePackage({ descriptors: [{
        ...descriptor,
        image: { ...descriptor.image!, url: 'https://baked.example/plot-v2.png', revision: 'v2', live: false },
        images: [{ ...descriptor.images![0], url: 'https://baked.example/plot-v2.png', revision: 'v2', live: false }],
      }] })} projectId="proj-1" />);
      await new Promise<void>((resolve) => setTimeout(resolve, 10));
    });
    expect(host!.querySelector('img')?.getAttribute('src')).toBe('https://fresh.example.com/plot-v2.png');
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(2);
  });

  test('falls back to the baked URL when no projectId is available', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-image',
          kind: 'image',
          title: 'Reward plot',
          status: 'ready',
          image: {
            url: 'https://storage.example.com/plot.png?sig=baked',
            alt: 'Reward plot',
            storage_uri: 'gs://bucket/proj/plot.png',
          },
          images: [
            {
              url: 'https://storage.example.com/plot.png?sig=baked',
              alt: 'plot.png',
              filename: 'plot.png',
              storage_uri: 'gs://bucket/proj/plot.png',
            },
          ],
        },
      ],
    });

    await render(pkg, null);

    expect(vi.mocked(resolveArtifactDownloadUrl)).not.toHaveBeenCalled();
    const img = host!.querySelector('img');
    expect(img).not.toBeNull();
    expect(img!.getAttribute('src')).toContain('sig=baked');
  });

  test('renders a pdf descriptor as a click-to-open button (no inline img/iframe) that opens a new tab', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-pdf',
          kind: 'pdf',
          title: 'Reward report',
          status: 'ready',
          pdf: { url: null, alt: 'report.pdf', storage_uri: 'gs://bucket/proj/report.pdf' },
          pdfs: [
            { url: null, alt: 'report.pdf', filename: 'report.pdf', storage_uri: 'gs://bucket/proj/report.pdf' },
          ],
        },
      ],
    });

    await render(pkg, 'proj-1');

    // No inline embed on the app origin.
    expect(host!.querySelector('img')).toBeNull();
    expect(host!.querySelector('iframe')).toBeNull();
    expect(host!.querySelector('embed')).toBeNull();
    const button = host!.querySelector('[data-testid="strategic-context-pdf-open"]');
    expect(button).not.toBeNull();
    expect(button!.tagName).toBe('BUTTON');
    expect(button!.textContent).toContain('report.pdf');

    // Clicking opens the freshly resolved signed URL in a new tab.
    await act(async () => { (button as HTMLButtonElement).click(); });
    expect(openSpy).toHaveBeenCalledWith(
      expect.stringContaining('fresh.example.com'),
      '_blank',
      'noopener,noreferrer',
    );
    openSpy.mockRestore();
  });

  function imageDescriptor(overrides: Partial<StrategicContextRenderableDescriptor> = {}): StrategicContextRenderableDescriptor {
    return {
      schema_version: 1,
      id: 'art-image',
      kind: 'image' as const,
      title: 'Reward plot',
      status: 'ready',
      image: { url: null, alt: 'Reward plot', storage_uri: 'gs://bucket/proj/plot.png' },
      images: [
        { url: null, alt: 'plot.png', filename: 'plot.png', storage_uri: 'gs://bucket/proj/plot.png' },
      ],
      ...overrides,
    };
  }

  test('shows a loading skeleton before the signed URL resolves, then the image', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    // Hold the first resolution open so we can observe the loading state.
    let settle: ((url: string) => void) | null = null;
    vi.mocked(resolveArtifactDownloadUrl).mockImplementationOnce(
      () => new Promise<string>((resolve) => { settle = resolve; }),
    );

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<StrategicContextArtifactSlot contextPackage={makePackage({ descriptors: [imageDescriptor()] })} projectId="proj-1" />);
    });
    // storage_uri-only + not-yet-resolved => skeleton, no img yet.
    expect(host.querySelector('[data-testid="strategic-artifact-loading"]')).not.toBeNull();
    expect(host.querySelector('img')).toBeNull();

    // Resolve the signed URL and let it flush.
    await act(async () => {
      settle?.('https://fresh.example.com/signed?ref=plot');
      await Promise.resolve();
    });
    expect(host.querySelector('[data-testid="strategic-artifact-loading"]')).toBeNull();
    expect(host.querySelector('img')).not.toBeNull();
  });

  test('an image load error auto-re-resolves once before surfacing the manual retry', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    const pkg = makePackage({ descriptors: [imageDescriptor()] });
    await render(pkg, 'proj-1');
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(1);

    const img = host!.querySelector('img');
    expect(img).not.toBeNull();

    // First error — should trigger an automatic re-resolve (no retry button yet).
    await act(async () => { img!.dispatchEvent(new Event('error')); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(2);
    // Auto-resolved: image is restored, no retry button.
    expect(host!.querySelector('img')).not.toBeNull();
    expect(host!.querySelector('[data-testid="strategic-artifact-retry"]')).toBeNull();

    // Second error (auto-retry already consumed) — now shows the manual retry button.
    const img2 = host!.querySelector('img');
    await act(async () => { img2!.dispatchEvent(new Event('error')); });
    const retry = host!.querySelector<HTMLButtonElement>('[data-testid="strategic-artifact-retry"]');
    expect(retry).not.toBeNull();
    expect(host!.querySelector('img')).toBeNull();

    // Clicking manual retry re-mints once more and restores the preview.
    await act(async () => { retry!.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(3);
    expect(host!.querySelector('img')).not.toBeNull();
  });

  test('clicking an image opens a full-size lightbox that Escape and backdrop close', async () => {
    const pkg = makePackage({ descriptors: [imageDescriptor()] });
    await render(pkg, 'proj-1');

    const thumb = host!.querySelector<HTMLButtonElement>('[data-testid="strategic-artifact-image"]');
    expect(thumb).not.toBeNull();
    await act(async () => { thumb!.click(); });

    let lightbox = host!.querySelector('[data-testid="strategic-artifact-lightbox"]');
    expect(lightbox).not.toBeNull();
    // Full-size image + "open in new tab" fallback are present.
    expect(lightbox!.querySelector('img')).not.toBeNull();
    const newTab = lightbox!.querySelector<HTMLAnchorElement>('[data-testid="strategic-artifact-lightbox-newtab"]');
    expect(newTab).not.toBeNull();
    expect(newTab!.getAttribute('href')).toContain('fresh.example.com');

    // Escape closes it.
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    expect(host!.querySelector('[data-testid="strategic-artifact-lightbox"]')).toBeNull();

    // Re-open and close via the backdrop click.
    await act(async () => { thumb!.click(); });
    lightbox = host!.querySelector('[data-testid="strategic-artifact-lightbox"]');
    expect(lightbox).not.toBeNull();
    await act(async () => { (lightbox as HTMLElement).click(); });
    expect(host!.querySelector('[data-testid="strategic-artifact-lightbox"]')).toBeNull();
  });

  test('a video load error auto-re-resolves once before surfacing the manual retry', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-video',
          kind: 'video',
          title: 'Rollout',
          status: 'ready',
          video: { url: null, alt: 'Rollout', storage_uri: 'gs://bucket/proj/rollout.mp4' },
          videos: [
            { url: null, alt: 'rollout.mp4', filename: 'rollout.mp4', storage_uri: 'gs://bucket/proj/rollout.mp4' },
          ],
        },
      ],
    });
    await render(pkg, 'proj-1');
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(1);

    const video = host!.querySelector('[data-testid="strategic-artifact-video"]');
    expect(video).not.toBeNull();

    // First error — auto-re-resolves without showing a retry button.
    await act(async () => { video!.dispatchEvent(new Event('error')); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(2);
    expect(host!.querySelector('[data-testid="strategic-artifact-video"]')).not.toBeNull();
    expect(host!.querySelector('[data-testid="strategic-artifact-retry"]')).toBeNull();

    // Second error — auto-retry consumed, shows manual retry.
    const video2 = host!.querySelector('[data-testid="strategic-artifact-video"]');
    await act(async () => { video2!.dispatchEvent(new Event('error')); });
    const retry = host!.querySelector<HTMLButtonElement>('[data-testid="strategic-artifact-retry"]');
    expect(retry).not.toBeNull();
    await act(async () => { retry!.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(3);
    expect(host!.querySelector('[data-testid="strategic-artifact-video"]')).not.toBeNull();
  });

  test('a multi-image gallery renders each image with an expand control', async () => {
    const pkg = makePackage({
      descriptors: [
        imageDescriptor({
          images: [
            { url: null, alt: 'plot-a.png', filename: 'plot-a.png', storage_uri: 'gs://bucket/proj/a.png' },
            { url: null, alt: 'plot-b.png', filename: 'plot-b.png', storage_uri: 'gs://bucket/proj/b.png' },
          ],
        }),
      ],
    });
    await render(pkg, 'proj-1');
    const thumbs = host!.querySelectorAll('[data-testid="strategic-artifact-image"]');
    expect(thumbs.length).toBe(2);
    expect(host!.querySelectorAll('img').length).toBe(2);
  });

  test('renders every mixed-media group when image remains the primary descriptor kind', async () => {
    const pkg = makePackage({
      descriptors: [
        imageDescriptor({
          videos: [
            { url: null, alt: 'rollout.mp4', filename: 'rollout.mp4', storage_uri: 'gs://bucket/proj/rollout.mp4' },
          ],
          pdfs: [
            { url: null, alt: 'report.pdf', filename: 'report.pdf', storage_uri: 'gs://bucket/proj/report.pdf' },
          ],
        }),
      ],
    });
    await render(pkg, 'proj-1');

    expect(host!.querySelector('[data-testid="strategic-artifact-image"]')).not.toBeNull();
    expect(host!.querySelector('[data-testid="strategic-artifact-video"]')).not.toBeNull();
    const pdfButton = host!.querySelector('[data-testid="strategic-context-pdf-open"]');
    expect(pdfButton).not.toBeNull();
    expect(pdfButton!.textContent).toContain('report.pdf');
  });

  test('renders an image that has only a storage_uri (expired/absent baked URL)', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-image-ref-only',
          kind: 'image',
          title: 'Reward plot',
          status: 'ready',
          image: { url: null, alt: 'Reward plot', storage_uri: 'gs://bucket/proj/plot.png' },
          images: [
            { url: null, alt: 'plot.png', filename: 'plot.png', storage_uri: 'gs://bucket/proj/plot.png' },
          ],
        },
      ],
    });

    await render(pkg, 'proj-1');

    const img = host!.querySelector('img');
    expect(img).not.toBeNull();
    expect(img!.getAttribute('src')).toContain('fresh.example.com');
  });

  test('renders the EvidenceTierBadge and run-provenance details for a compute artifact', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art:compute-1',
          kind: 'resource_detail',
          title: 'RL training run',
          status: 'completed',
          evidence_tier: 'covered',
          details: [
            { label: 'Executed on', value: 'local-runner' },
            { label: 'Runner', value: 'gpu-box-01 (runner-42)' },
          ],
        },
      ],
    });

    await render(pkg, 'proj-1');

    // The shared badge renders the canonical tier label.
    expect(host!.textContent).toContain('Machine verified');
    // Run provenance surfaces via the generic details list.
    expect(host!.textContent).toContain('Executed on');
    expect(host!.textContent).toContain('local-runner');
    expect(host!.textContent).toContain('Runner');
    expect(host!.textContent).toContain('gpu-box-01 (runner-42)');
  });

  test('omits the EvidenceTierBadge for a descriptor without an evidence_tier', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art:plain-1',
          kind: 'resource_detail',
          title: 'Plain artifact',
          status: 'ready',
          details: [{ label: 'Type', value: 'design_doc' }],
        },
      ],
    });

    await render(pkg, 'proj-1');

    expect(host!.textContent).not.toContain('Machine verified');
    expect(host!.textContent).not.toContain('Self-reported');
  });

  test('renders an amber omitted-files note when viewable_files_dropped > 0 for an image descriptor', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-dropped',
          kind: 'image',
          title: 'Training plot',
          status: 'ready',
          image: { url: 'https://storage.example.com/plot.png', alt: 'Training plot' },
          images: [
            { url: 'https://storage.example.com/plot.png', alt: 'plot.png', filename: 'plot.png' },
          ],
          viewable_files_dropped: 2,
        },
      ],
    });

    await render(pkg, 'proj-1');

    const note = host!.querySelector('[data-testid="strategic-artifact-omitted-note"]');
    expect(note).not.toBeNull();
    expect(note!.textContent).toContain('2 files omitted');
    expect(note!.className).toContain('text-amber-400/90');
  });

  test('renders omitted note for a pdf descriptor as well', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-pdf-dropped',
          kind: 'pdf',
          title: 'Report',
          status: 'ready',
          pdf: { url: null, alt: 'report.pdf', storage_uri: 'gs://bucket/proj/report.pdf' },
          pdfs: [
            { url: null, alt: 'report.pdf', filename: 'report.pdf', storage_uri: 'gs://bucket/proj/report.pdf' },
          ],
          viewable_files_dropped: 5,
        },
      ],
    });

    await render(pkg, null);

    const note = host!.querySelector('[data-testid="strategic-artifact-omitted-note"]');
    expect(note).not.toBeNull();
    expect(note!.textContent).toContain('5 files omitted');
  });

  test('omits the omitted-files note when viewable_files_dropped is absent', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-no-drops',
          kind: 'image',
          title: 'Training plot',
          status: 'ready',
          image: { url: 'https://storage.example.com/plot.png', alt: 'Training plot' },
          images: [
            { url: 'https://storage.example.com/plot.png', alt: 'plot.png', filename: 'plot.png' },
          ],
        },
      ],
    });

    await render(pkg, 'proj-1');

    expect(host!.querySelector('[data-testid="strategic-artifact-omitted-note"]')).toBeNull();
  });

  test('omits the omitted-files note when descriptor kind is not media', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-non-media',
          kind: 'resource_detail',
          title: 'A report',
          status: 'ready',
          details: [{ label: 'Type', value: 'design_doc' }],
          // Even with a dropped count, non-media kinds should not show the note.
          viewable_files_dropped: 3,
        },
      ],
    });

    await render(pkg, 'proj-1');

    expect(host!.querySelector('[data-testid="strategic-artifact-omitted-note"]')).toBeNull();
  });
});

describe('ArtifactViewablePreviews — inline artifact-evidence thumbnails', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    const r = root;
    if (r) { act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function render(
    previews: StrategicContextArtifactViewablePreview[] | null | undefined,
    projectId?: string | null,
  ) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<ArtifactViewablePreviews previews={previews} projectId={projectId} label="Rollout" />);
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
  }

  test('renders an <img> for an image preview, re-resolved from storage_uri', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    await render(
      [
        {
          url: 'https://storage.example.com/plot.png?sig=stale',
          kind: 'image',
          filename: 'plot.png',
          storage_uri: 'gs://bucket/proj/plot.png',
        },
      ],
      'proj-1',
    );

    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledWith(
      'proj-1',
      'gs://bucket/proj/plot.png',
      expect.anything(),
    );
    const img = host!.querySelector('img');
    expect(img).not.toBeNull();
    // Fresh signed URL, not the stale baked one.
    expect(img!.getAttribute('src')).toContain('fresh.example.com');
    expect(img!.getAttribute('src')).not.toContain('sig=stale');
  });

  test('renders a <video> for a video preview', async () => {
    await render(
      [
        {
          url: 'https://storage.example.com/rollout.mp4?sig=baked',
          kind: 'video',
          filename: 'rollout.mp4',
          storage_uri: 'gs://bucket/proj/rollout.mp4',
        },
      ],
      'proj-1',
    );

    const video = host!.querySelector('video');
    expect(video).not.toBeNull();
    expect(video!.getAttribute('src')).toContain('fresh.example.com');
  });

  test('renders nothing when there are no previews', async () => {
    await render([], 'proj-1');
    expect(host!.querySelector('[data-testid="strategic-artifact-viewable-previews"]')).toBeNull();
    expect(host!.querySelector('img')).toBeNull();
  });

  test('renders nothing when previews is null', async () => {
    await render(null, 'proj-1');
    expect(host!.querySelector('[data-testid="strategic-artifact-viewable-previews"]')).toBeNull();
  });

  test('ignores non-image/video kinds and previews with neither url nor storage_uri', async () => {
    await render(
      [
        { url: '', kind: 'image', filename: 'empty.png', storage_uri: null },
        { url: 'https://storage.example.com/doc.pdf?sig=baked', kind: 'pdf', filename: 'doc.pdf', storage_uri: null },
      ],
      'proj-1',
    );
    // Neither entry is renderable → the strip self-hides entirely.
    expect(host!.querySelector('[data-testid="strategic-artifact-viewable-previews"]')).toBeNull();
  });

  test('falls back to the baked URL when no projectId is available', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    await render(
      [
        {
          url: 'https://storage.example.com/plot.png?sig=baked',
          kind: 'image',
          filename: 'plot.png',
          storage_uri: 'gs://bucket/proj/plot.png',
        },
      ],
      null,
    );
    expect(vi.mocked(resolveArtifactDownloadUrl)).not.toHaveBeenCalled();
    const img = host!.querySelector('img');
    expect(img).not.toBeNull();
    expect(img!.getAttribute('src')).toContain('sig=baked');
  });

  test('renders LIVE badge on an image preview with live=true', async () => {
    await render(
      [
        {
          url: 'https://storage.example.com/plot.png?sig=baked',
          kind: 'image',
          filename: 'plot.png',
          storage_uri: 'gs://bucket/proj/plot.png',
          live: true,
        },
      ],
      'proj-1',
    );

    const badge = host!.querySelector('[data-testid="live-badge"]');
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toContain('LIVE');
  });

  test('renders LIVE badge on a video preview with live=true', async () => {
    await render(
      [
        {
          url: 'https://storage.example.com/rollout.mp4?sig=baked',
          kind: 'video',
          filename: 'rollout.mp4',
          storage_uri: 'gs://bucket/proj/rollout.mp4',
          live: true,
        },
      ],
      'proj-1',
    );

    const badge = host!.querySelector('[data-testid="live-badge"]');
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toContain('LIVE');
  });

  test('does not render LIVE badge when live is false or absent', async () => {
    await render(
      [
        {
          url: 'https://storage.example.com/plot.png?sig=baked',
          kind: 'image',
          filename: 'plot.png',
          storage_uri: 'gs://bucket/proj/plot.png',
          live: false,
        },
      ],
      'proj-1',
    );

    expect(host!.querySelector('[data-testid="live-badge"]')).toBeNull();
  });
});

describe('StrategicContextArtifactSlot — line chart descriptor', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function render(pkg: StrategicContextPackageDescriptor, projectId?: string | null) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<StrategicContextArtifactSlot contextPackage={pkg} projectId={projectId} />);
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 0)); });
  }

  test('renders SVG polyline for a line chart descriptor with points', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'chart-line',
          kind: 'chart',
          title: 'Eval reward',
          status: 'ready',
          chart: {
            kind: 'line',
            y_label: 'eval_reward',
            x_label: 'time',
            points: [
              { label: '0.5', value: 0.5, t: '2026-01-01T00:00:00' },
              { label: '0.8', value: 0.8, t: '2026-01-01T01:00:00' },
              { label: '0.9', value: 0.9, t: '2026-01-01T02:00:00' },
            ],
          },
        },
      ],
    });

    await render(pkg, 'proj-1');

    // polyline lives in the LineChart SVG (not the BarChart3 icon SVG)
    const polylines = host!.querySelectorAll('polyline');
    expect(polylines.length).toBe(1);
    expect(polylines[0].getAttribute('points')).toBeTruthy();
    expect(host!.textContent).toContain('eval_reward');
    expect(host!.textContent).toContain('3 samples');
  });

  test('renders fallback for bar chart kind (not line)', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'chart-bar',
          kind: 'chart',
          title: 'Count',
          status: 'ready',
          chart: {
            kind: 'bar',
            points: [{ label: 'A', value: 10 }, { label: 'B', value: 20 }],
          },
        },
      ],
    });

    await render(pkg, 'proj-1');

    // Bar chart renders horizontal bars (not SVG polyline sparkline)
    const polyline = host!.querySelector('polyline');
    expect(polyline).toBeNull();
    expect(host!.textContent).toContain('A');
    expect(host!.textContent).toContain('B');
    expect(host!.textContent).toContain('10');
  });

  test('renders empty state for line chart with no points', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'chart-empty',
          kind: 'chart',
          title: 'Empty',
          status: 'ready',
          chart: {
            kind: 'line',
            points: [],
          },
        },
      ],
    });

    await render(pkg, 'proj-1');

    expect(host!.textContent).toContain('No chart points yet');
  });
});

describe('StrategicContextArtifactSlot — line chart descriptor', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function render(pkg: StrategicContextPackageDescriptor, projectId?: string | null) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<StrategicContextArtifactSlot contextPackage={pkg} projectId={projectId} />);
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 0)); });
  }

  test('renders SVG polyline for a line chart descriptor with points', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'chart-line',
          kind: 'chart',
          title: 'Eval reward',
          status: 'ready',
          chart: {
            kind: 'line',
            y_label: 'eval_reward',
            x_label: 'time',
            points: [
              { label: '0.5', value: 0.5, t: '2026-01-01T00:00:00' },
              { label: '0.8', value: 0.8, t: '2026-01-01T01:00:00' },
              { label: '0.9', value: 0.9, t: '2026-01-01T02:00:00' },
            ],
          },
        },
      ],
    });

    await render(pkg, 'proj-1');

    // polyline lives in the LineChart SVG (not the BarChart3 icon SVG)
    const polylines = host!.querySelectorAll('polyline');
    expect(polylines.length).toBe(1);
    expect(polylines[0].getAttribute('points')).toBeTruthy();
    expect(host!.textContent).toContain('eval_reward');
    expect(host!.textContent).toContain('3 samples');
  });

  test('renders fallback for bar chart kind (not line)', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'chart-bar',
          kind: 'chart',
          title: 'Count',
          status: 'ready',
          chart: {
            kind: 'bar',
            points: [{ label: 'A', value: 10 }, { label: 'B', value: 20 }],
          },
        },
      ],
    });

    await render(pkg, 'proj-1');

    // Bar chart renders horizontal bars (not SVG polyline sparkline)
    const polyline = host!.querySelector('polyline');
    expect(polyline).toBeNull();
    expect(host!.textContent).toContain('A');
    expect(host!.textContent).toContain('B');
    expect(host!.textContent).toContain('10');
  });

  test('renders empty state for line chart with no points', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'chart-empty',
          kind: 'chart',
          title: 'Empty',
          status: 'ready',
          chart: {
            kind: 'line',
            points: [],
          },
        },
      ],
    });

    await render(pkg, 'proj-1');

    expect(host!.textContent).toContain('No chart points yet');
  });
});
