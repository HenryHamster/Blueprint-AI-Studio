import { afterEach, describe, expect, test } from 'vitest';
import { page } from 'vitest/browser';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { WorkbenchPanel, WorkbenchSplit, WorkbenchSurface } from './WorkbenchLayout';
import { expectMinTouchTargetAt, expectNoOverflowAt } from '../test/viewport';

describe('WorkbenchLayout viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
    }
    root = null;
    host?.remove();
    host = null;
  });

  async function renderWorkbench() {
    host = document.createElement('div');
    host.style.height = '720px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <WorkbenchSurface
          title="Strategic"
          subtitle="current project"
          tabs={[
            { id: 'chat', label: 'Chat' },
            { id: 'pipeline', label: 'Pipeline' },
            { id: 'runtime', label: 'Runtime' },
          ]}
          activeTab="chat"
          onTabChange={() => {}}
          actions={<button type="button" className="min-h-11 rounded-md border border-neutral-800 px-3 md:min-h-8">Refresh</button>}
        >
          <WorkbenchSplit
            main={(
              <div className="min-h-0 rounded-md border border-neutral-800 bg-workbench-layer p-3 text-sm text-neutral-200">
                Main work area
              </div>
            )}
            rail={(
              <WorkbenchPanel title="Context" meta="live">
                <button type="button" className="min-h-11 rounded-md border border-neutral-800 px-3">
                  Details
                </button>
              </WorkbenchPanel>
            )}
          />
        </WorkbenchSurface>,
      );
    });
  }

  test('mobile workbench shell fits without overflow and preserves touch targets', async () => {
    await expectNoOverflowAt(375, renderWorkbench);
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('desktop rail and panel use compact workbench padding', async () => {
    await page.viewport(1280, 800);
    await renderWorkbench();

    const rail = host?.querySelector('aside');
    const panel = rail?.querySelector('section');
    const panelBody = panel?.lastElementChild;
    expect(rail ? getComputedStyle(rail).paddingTop : '').toBe('6px');
    expect(panelBody instanceof HTMLElement ? getComputedStyle(panelBody).paddingTop : '').toBe('6px');
  });

  test('action-only surface hides the duplicate title while preserving actions', async () => {
    await page.viewport(1280, 800);
    host = document.createElement('div');
    host.style.height = '320px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    let actionOpened = false;
    await act(async () => {
      r.render(
        <WorkbenchSurface
          title="Strategic"
          showTitle={false}
          actions={(
            <button type="button" onClick={() => { actionOpened = true; }}>
              Command
            </button>
          )}
          flush
        >
          <div>Chat surface</div>
        </WorkbenchSurface>,
      );
    });

    expect(host.querySelector('h1')).toBeNull();
    const action = host.querySelector<HTMLButtonElement>('button');
    expect(action?.textContent).toContain('Command');
    await act(async () => { action?.click(); });
    expect(actionOpened).toBe(true);
  });

  test('document root keeps the dark shell anchored during scroll', async () => {
    await page.viewport(390, 700);
    const appRoot = document.createElement('div');
    appRoot.id = 'root';
    const appShell = document.createElement('div');
    appShell.className = 'flex min-h-screen flex-col bg-[#08090b] text-neutral-200';
    const tallContent = document.createElement('main');
    tallContent.style.minHeight = '1400px';
    appShell.appendChild(tallContent);
    appRoot.appendChild(appShell);
    document.body.appendChild(appRoot);

    try {
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve());
      });
      window.scrollTo(0, document.documentElement.scrollHeight);
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve());
      });

      const workbenchBase = 'rgb(3, 6, 10)';
      expect(getComputedStyle(document.documentElement).backgroundColor).toBe(workbenchBase);
      expect(getComputedStyle(document.body).backgroundColor).toBe(workbenchBase);
      expect(getComputedStyle(appRoot).backgroundColor).toBe(workbenchBase);
      expect(getComputedStyle(appShell).backgroundColor).toBe('rgb(8, 9, 11)');
      expect(appRoot.getBoundingClientRect().height).toBeGreaterThanOrEqual(1400);
      expect(appShell.getBoundingClientRect().height).toBeGreaterThanOrEqual(1400);
    } finally {
      appRoot.remove();
      window.scrollTo(0, 0);
    }
  });
});
