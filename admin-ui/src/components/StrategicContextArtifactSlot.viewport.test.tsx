import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';
import { StrategicContextArtifactSlot } from './StrategicContextArtifactSlot';
import type { StrategicContextPackageDescriptor } from '../strategicStateApi';

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
