import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';
import { StrategicContextArtifactSlot } from './StrategicContextArtifactSlot';
import type { StrategicContextPackageDescriptor, StrategicContextArtifactViewablePreview } from '../strategicStateApi';

function makePackage(overrides: Partial<StrategicContextPackageDescriptor> = {}): StrategicContextPackageDescriptor {
  return {
    id: 'pkg-test',
    title: 'Training run results',
    summary: 'Compute experiment completed successfully.',
    status: 'ready',
    descriptors: [],
    ...overrides,
  };
}

describe('StrategicContextArtifactSlot — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders single image descriptor at 900px', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-single',
          kind: 'image',
          title: 'Reward plot',
          summary: 'Reward over training steps',
          status: 'ready',
          image: { url: 'https://storage.example.com/plot.png?sig=abc', alt: 'Reward plot' },
          images: [
            { url: 'https://storage.example.com/plot.png?sig=abc', alt: 'plot.png', filename: 'plot.png' },
          ],
        },
      ],
    });
    await expectFullViewportAt(
      900,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<StrategicContextArtifactSlot contextPackage={pkg} />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 30)); });
        const text = host.textContent ?? '';
        expect(text).toContain('Reward plot');
      },
      { waitMs: 40 },
    );
  });

  test('renders multi-image gallery descriptor at 900px', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-multi',
          kind: 'image',
          title: 'Multi-arm reward plots',
          summary: 'Per-arm reward curves',
          status: 'ready',
          image: { url: 'https://storage.example.com/arm0/reward.png?sig=a1', alt: 'arm0/reward.png' },
          images: [
            { url: 'https://storage.example.com/arm0/reward.png?sig=a1', alt: 'arm0/reward.png', filename: 'arm0/reward.png' },
            { url: 'https://storage.example.com/arm1/reward.png?sig=a2', alt: 'arm1/reward.png', filename: 'arm1/reward.png' },
            { url: 'https://storage.example.com/arm2/reward.png?sig=a3', alt: 'arm2/reward.png', filename: 'arm2/reward.png' },
          ],
        },
      ],
    });
    await expectFullViewportAt(
      900,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<StrategicContextArtifactSlot contextPackage={pkg} />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 30)); });
        const text = host.textContent ?? '';
        expect(text).toContain('Multi-arm reward plots');
        // All three images should be rendered in the gallery
        const imgs = host.querySelectorAll('img');
        expect(imgs.length).toBeGreaterThanOrEqual(3);
      },
      { waitMs: 40 },
    );
  });

  test('renders video descriptor built from viewable_previews at 900px', async () => {
    // Simulates the descriptor shape produced when strategic_context_package.py
    // populates viewable_previews on an ArtifactEvidence item and the descriptor
    // builder converts them into descriptor.videos entries.
    const previews: StrategicContextArtifactViewablePreview[] = [
      { url: 'https://storage.example.com/run.mp4?sig=v1', kind: 'video', filename: 'run.mp4' },
    ];
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-video',
          kind: 'video',
          title: 'Training run video',
          summary: 'Captured from compute job',
          status: 'ready',
          video: { url: previews[0].url, alt: previews[0].filename ?? undefined },
          videos: previews.map(p => ({ url: p.url, alt: p.filename ?? undefined, filename: p.filename ?? undefined })),
        },
      ],
    });
    await expectFullViewportAt(
      900,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<StrategicContextArtifactSlot contextPackage={pkg} />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 30)); });
        const text = host.textContent ?? '';
        expect(text).toContain('Training run video');
        const videos = host.querySelectorAll('video');
        expect(videos.length).toBeGreaterThanOrEqual(1);
      },
      { waitMs: 40 },
    );
  });

  test('renders image + video media previews at 1280px without overflow (full-size affordance present)', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-image',
          kind: 'image',
          title: 'Reward-over-time plot for the long-running rollout experiment',
          summary: 'Reward over training steps for every arm in the sweep.',
          status: 'ready',
          image: { url: 'https://storage.example.com/plot.png?sig=baked', alt: 'Reward over time' },
          images: [
            { url: 'https://storage.example.com/a.png?sig=baked', alt: 'plot-a.png', filename: 'plot-a.png' },
            { url: 'https://storage.example.com/b.png?sig=baked', alt: 'plot-b.png', filename: 'plot-b.png' },
          ],
        },
        {
          schema_version: 1,
          id: 'art-video',
          kind: 'video',
          title: 'Rollout episode recording',
          summary: 'Captured from the compute job bundle.',
          status: 'ready',
          video: { url: 'https://storage.example.com/rollout.mp4?sig=baked', alt: 'Rollout' },
          videos: [
            { url: 'https://storage.example.com/rollout.mp4?sig=baked', alt: 'rollout.mp4', filename: 'rollout.mp4' },
          ],
        },
      ],
    });
    await expectFullViewportAt(
      1280,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<StrategicContextArtifactSlot contextPackage={pkg} />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 30)); });
        // The image is now a click-to-expand full-size control.
        const thumbs = host.querySelectorAll('[data-testid="strategic-artifact-image"]');
        expect(thumbs.length).toBeGreaterThanOrEqual(2);
        expect(host.querySelectorAll('video').length).toBeGreaterThanOrEqual(1);
      },
      { waitMs: 40 },
    );
  });

  test('renders image descriptor without URL (stub) at 900px', async () => {
    const pkg = makePackage({
      descriptors: [
        {
          schema_version: 1,
          id: 'art-stub',
          kind: 'image',
          title: 'Pending image',
          summary: null,
          status: 'ready',
          unsupported_reason: 'Image artifact previews require a signed preview URL.',
          image: { url: null, alt: 'Pending image' },
          images: [],
        },
      ],
    });
    await expectFullViewportAt(
      900,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<StrategicContextArtifactSlot contextPackage={pkg} />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 30)); });
        const text = host.textContent ?? '';
        expect(text).toContain('Pending image');
      },
      { waitMs: 40 },
    );
  });
});
