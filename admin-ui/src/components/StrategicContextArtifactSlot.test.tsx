import { afterEach, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicContextArtifactSlot } from './StrategicContextArtifactSlot';
import type { StrategicContextPackageDescriptor, StrategicContextRenderableDescriptor } from '../strategicStateApi';

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

  test('an image load error surfaces a retry that re-mints the signed URL', async () => {
    const { resolveArtifactDownloadUrl } = await import('../api');
    const pkg = makePackage({ descriptors: [imageDescriptor()] });
    await render(pkg, 'proj-1');
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(1);

    const img = host!.querySelector('img');
    expect(img).not.toBeNull();
    // Simulate the signed URL going dead — the <img> fails to load.
    await act(async () => { img!.dispatchEvent(new Event('error')); });

    // The image is replaced by a retry affordance.
    const retry = host!.querySelector<HTMLButtonElement>('[data-testid="strategic-artifact-retry"]');
    expect(retry).not.toBeNull();
    expect(host!.querySelector('img')).toBeNull();

    // Clicking retry re-mints the URL and restores the preview.
    await act(async () => { retry!.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(2);
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

  test('a video load error surfaces a retry that re-mints the signed URL', async () => {
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
    await act(async () => { video!.dispatchEvent(new Event('error')); });

    const retry = host!.querySelector<HTMLButtonElement>('[data-testid="strategic-artifact-retry"]');
    expect(retry).not.toBeNull();
    await act(async () => { retry!.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 10)); });
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledTimes(2);
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
});
