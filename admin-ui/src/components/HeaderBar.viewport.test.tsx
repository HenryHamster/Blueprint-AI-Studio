import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { HeaderBar, PrimaryTabNav } from './HeaderBar';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost',
  authHeaders: vi.fn(async () => ({})),
}));

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

// AutonomousStatus is a complex sub-tree; stub it to keep the test focused on
// HeaderBar's own mobile layout.
vi.mock('./AutonomousStatus', () => ({
  AutonomousStatus: () => <div data-testid="autonomous-status" />,
}));

const noop = () => {};

function makeProps() {
  return {
    tasks: [],
    onRefresh: noop,
    showResearchBrowser: false,
    onToggleResearchBrowser: noop,
    showKnowledgeBrowser: false,
    onToggleKnowledgeBrowser: noop,
    openMessageCount: 0,
    onOpenNewTaskModal: noop,
    onOpenSettings: noop,
    onSignOut: noop,
    userEmail: 'test@example.com',
    wsRef: { current: null },
    apiHealthy: true,
    wsConnected: true,
    lastFetchTime: 0,
    deployState: null,
    selectedProjectId: null,
    projects: [],
    onSelectProject: noop,
    activeTab: 'chat' as const,
    onTabChange: noop,
    health: null,
    detailedHealth: null,
  };
}

describe('HeaderBar — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('collapsed mobile hamburger state renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
    });
  });

  test('expanded mobile menu renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      // Give enough height so the absolute dropdown doesn't extend below viewport
      host.style.minHeight = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
      // Click the mobile menu hamburger button to open the dropdown
      const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
      if (hamburger) {
        await act(async () => { hamburger.click(); });
      }
    });
  });

  test('mobile menu buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.minHeight = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
      // Open the mobile menu to expose its buttons
      const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
      if (hamburger) {
        await act(async () => { hamburger.click(); });
      }
    });
  });

  test('header text labels render without silent clipping at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
    });
  });

  test('desktop layout renders without overflow at 768px (md: breakpoint)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
    });
  });

  test('desktop top bar keeps journal, library, and activity out of primary chrome', async () => {
    const onOpenActivityFeed = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <HeaderBar
          {...makeProps()}
          showResearchBrowser
          showKnowledgeBrowser
          onOpenActivityFeed={onOpenActivityFeed}
        />,
      );
    });

    expect(host.querySelector<HTMLButtonElement>('button[title="Findings journal"]')).toBeNull();
    expect(host.querySelector<HTMLButtonElement>('button[title="Knowledge library"]')).toBeNull();
    expect(host.querySelector<HTMLButtonElement>('button[title="Chronological activity stream"]')).toBeNull();
    expect(host.textContent).not.toContain('Activity stream');
    expect(onOpenActivityFeed).not.toHaveBeenCalled();
  });

  test('autonomy control mounts once and is present on desktop (no md:hidden gating)', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} />); });

    // Single responsive instance shared by desktop + mobile — no duplicate mount.
    const controls = host.querySelectorAll('[data-testid="header-autonomy-control"]');
    expect(controls).toHaveLength(1);
    const statuses = host.querySelectorAll('[data-testid="autonomous-status"]');
    expect(statuses).toHaveLength(1);
    // Wrapper must NOT be gated behind md:hidden (which hid it on desktop before).
    expect(controls[0].className).not.toContain('md:hidden');
  });

  test('desktop settings and sign-out actions use header callbacks', async () => {
    const onOpenSettings = vi.fn();
    const onSignOut = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} onOpenSettings={onOpenSettings} onSignOut={onSignOut} />);
    });

    const settingsButton = host.querySelector<HTMLButtonElement>('button[title="Runtime settings"]');
    const signOutButton = host.querySelector<HTMLButtonElement>('button[title="Sign out test@example.com"]');
    expect(settingsButton).toBeTruthy();
    expect(signOutButton).toBeTruthy();

    await act(async () => { settingsButton?.click(); });
    await act(async () => { signOutButton?.click(); });

    expect(onOpenSettings).toHaveBeenCalledOnce();
    expect(onSignOut).toHaveBeenCalledOnce();
  });

  test('desktop command search opens the command palette from the global header', async () => {
    const onOpenCommandPalette = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} activeTab="runtime" onOpenCommandPalette={onOpenCommandPalette} />);
    });
    const commandButton = host.querySelector<HTMLButtonElement>('[data-testid="header-command-search"]');
    expect(commandButton).toBeTruthy();
    expect(commandButton?.textContent).toContain('Ctrl K');
    expect(commandButton?.textContent).not.toContain('Search commands');
    expect(commandButton?.textContent).not.toContain('Message Strategic');
    await act(async () => { commandButton?.click(); });
    expect(onOpenCommandPalette).toHaveBeenCalledOnce();
  });

  test('desktop workspace navigation sits to the right of command search', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onOpenCommandPalette={vi.fn()} />); });

    const commandButton = host.querySelector<HTMLButtonElement>('[data-testid="header-command-search"]');
    const primaryNav = host.querySelector<HTMLElement>('[data-testid="primary-tab-nav"]');
    const commandSlot = commandButton?.parentElement;
    expect(commandButton).toBeTruthy();
    expect(primaryNav).toBeTruthy();
    expect(commandSlot?.nextElementSibling).toBe(primaryNav?.parentElement);
    expect(primaryNav?.parentElement?.className).toContain('ml-auto');
  });

  test('desktop header keeps the Blueprint brand icon-only and project switcher primary', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} />); });

    expect(host.textContent).not.toContain('Blueprint');
    expect(host.querySelector('[aria-label="Blueprint"]')).not.toBeNull();
  });

  test('primary mode label is Chat, not a duplicate Strategic entry point', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} activeTab="chat" />); });

    const chatTab = Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Chat');
    expect(chatTab).toBeTruthy();
    const oldStrategicTab = Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Strategic');
    expect(oldStrategicTab).toBeUndefined();
  });

  test('mobile menu exposes a real command palette launcher outside Chat composer', async () => {
    const onOpenCommandPalette = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} activeTab="runtime" onOpenCommandPalette={onOpenCommandPalette} />);
    });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const commandButton = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Command palette'));
    expect(commandButton).toBeTruthy();
    await act(async () => { commandButton?.click(); });
    expect(onOpenCommandPalette).toHaveBeenCalledOnce();
  });

  test('mobile Chat menu surfaces the command palette', async () => {
    const onOpenCommandPalette = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} activeTab="chat" onOpenCommandPalette={onOpenCommandPalette} />);
    });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const commandButton = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Command palette'));
    expect(commandButton).toBeTruthy();
    await act(async () => { commandButton?.click(); });
    expect(onOpenCommandPalette).toHaveBeenCalledOnce();
  });

  test('global header does not expose permanent New task chrome outside Pipeline', async () => {
    const onOpenNewTaskModal = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} activeTab="chat" onOpenNewTaskModal={onOpenNewTaskModal} />);
    });
    const desktopNewTask = host.querySelector<HTMLButtonElement>('button[title="New task"]');
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });
    const mobileNewTask = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'New task');

    expect(desktopNewTask).toBeNull();
    expect(mobileNewTask).toBeUndefined();
    expect(onOpenNewTaskModal).not.toHaveBeenCalled();
  });

  test('mobile menu does not duplicate primary mode navigation', async () => {
    const onTabChange = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onTabChange={onTabChange} />); });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const menu = hamburger?.nextElementSibling;
    const modeButtons = Array.from(menu?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .filter(button => ['Chat', 'Pipeline', 'Project', 'Runtime'].includes(button.textContent?.trim() ?? ''));

    expect(modeButtons).toHaveLength(0);
    expect(onTabChange).not.toHaveBeenCalled();
  });

  test('mobile menu exposes health panel without a separate monitoring surface', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const healthButton = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Health panel'));
    expect(healthButton).toBeTruthy();
    await act(async () => { healthButton?.click(); });

    expect(host.querySelector('[data-testid="orchestrator-health-bar"]')).not.toBeNull();
    expect(host.textContent).not.toContain('Task stream');
  });

  test('mobile menu routes settings and sign out through explicit actions', async () => {
    const onOpenSettings = vi.fn();
    const onSignOut = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} onOpenSettings={onOpenSettings} onSignOut={onSignOut} />);
    });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const menuButtons = Array.from(hamburger?.nextElementSibling?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    const settingsButton = menuButtons.find(button => button.textContent?.trim() === 'Runtime settings');
    const signOutButton = menuButtons.find(button => button.textContent?.trim() === 'Sign out');

    await act(async () => { settingsButton?.click(); });
    await act(async () => {
      hamburger?.click();
    });
    const reopenedButtons = Array.from(hamburger?.nextElementSibling?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    await act(async () => {
      reopenedButtons.find(button => button.textContent?.trim() === 'Sign out')?.click();
    });

    expect(settingsButton).toBeTruthy();
    expect(signOutButton).toBeTruthy();
    expect(onOpenSettings).toHaveBeenCalledOnce();
    expect(onSignOut).toHaveBeenCalledOnce();
  });

  test('mobile menu hides new task while Pipeline owns task creation controls', async () => {
    const onOpenNewTaskModal = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onOpenNewTaskModal={onOpenNewTaskModal} activeTab="pipeline" />); });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const mobileMenu = hamburger?.nextElementSibling;
    const mobileNewTaskButton = Array.from(mobileMenu?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find(button => button.textContent?.trim() === 'New task');
    expect(mobileNewTaskButton).toBeUndefined();
    expect(onOpenNewTaskModal).not.toHaveBeenCalled();
  });

  test('mobile primary nav switches modes and shows badges', async () => {
    const onTabChange = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <PrimaryTabNav
          activeTab="chat"
          onTabChange={onTabChange}
          openMessageCount={3}
          needsAttentionCount={5}
          placement="mobile"
        />,
      );
    });

    const buttons = Array.from(host.querySelectorAll<HTMLButtonElement>('button'));
    expect(buttons.map(button => button.textContent?.trim())).toEqual([
      'Chat3',
      'Pipeline5',
      'Project',
      'Runtime',
    ]);
    await act(async () => { buttons[1].click(); });
    expect(onTabChange).toHaveBeenCalledWith('pipeline');
  });

  test('pipeline tab shows attention badge from existing count prop', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} needsAttentionCount={7} />);
    });

    const pipelineTab = Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Pipeline'));
    expect(pipelineTab?.textContent).toContain('7');
  });

});
