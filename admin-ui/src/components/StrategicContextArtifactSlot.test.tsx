import { afterEach, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicContextArtifactSlot } from './StrategicContextArtifactSlot';
import type { StrategicContextPackageDescriptor } from '../strategicStateApi';

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
