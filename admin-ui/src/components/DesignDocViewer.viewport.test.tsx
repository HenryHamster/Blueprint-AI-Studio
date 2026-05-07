import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import DesignDocViewer from './DesignDocViewer';
import { expectNoOverflowAt } from '../test/viewport';

// DesignDocViewer is pure prop-driven (no '../api' import) so no module
// mocking is required. The component renders a sidebar + content area
// inside a flex h-full shell — at 375px the sidebar is 180px wide and
// the content area takes the remainder.

const sampleDoc: Record<string, unknown> = {
  _meta: { revision: 4, last_updated: '2026-05-06T00:00:00Z' },
  overview: 'A short string section that should render as markdown content.',
  architecture: {
    status: 'mature',
    description: 'Pipeline-based orchestration with self-improving agents.',
    components: ['orchestrator', 'agent', 'admin-ui'],
  },
  'features/auth': { status: 'functional', name: 'Authentication' },
  'features/billing': { status: 'partial', name: 'Billing' },
};

describe('DesignDocViewer — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('overview renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      // Component uses h-full; give the host a finite height so flex children lay out.
      host.style.height = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DesignDocViewer designDoc={sampleDoc} />);
      });
    });
  });

  test('empty design doc renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.height = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DesignDocViewer designDoc={{}} />);
      });
    });
  });
});
