import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchProjectTools: vi.fn(),
  };
});

import { fetchProjectTools } from '../api/projectTools';
import { WhiteboardRailSection } from './WhiteboardRailSection';

const WHITEBOARD_TOOL = {
  id: 'wb-1',
  kind: 'whiteboard',
  slug: 'whiteboard',
  title: 'Whiteboard',
  enabled: true,
  placement: 'sidebar' as const,
  content_revision: 1,
};

describe('WhiteboardRailSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.resetAllMocks();
  });

  test('renders the open-canvas entry point without overflow at 375px', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue({
      tools: [WHITEBOARD_TOOL],
      total_count: 1,
      next_offset: null,
    });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<WhiteboardRailSection projectId="proj-a" />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Open canvas');
    await expectNoTextTruncationAt(375, async () => {});
  });

  test('renders the "add in Project → Tools" hint when no whiteboard exists at 375px', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue({
      tools: [],
      total_count: 0,
      next_offset: null,
    });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<WhiteboardRailSection projectId="proj-a" />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Project → Tools');
  });
});
