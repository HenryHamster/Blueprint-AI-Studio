/**
 * Tests for the PrimaryTabNav cross-tab alert indicator (PR #15230).
 *
 * The `activeAlerts` prop renders a red dot on every tab button when
 * alerts are present — including tabs the user is not currently on.
 * This is the "cross-tab" signal: an alert arriving while the user
 * is on the Pipeline tab produces a visible indicator on Chat, Project,
 * and Runtime tabs as well.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { PrimaryTabNav } from './HeaderBar';

// HeaderBar.tsx transitively imports AutonomousStatus which uses Firebase.
// Stub it so the module graph loads without a real Firebase project.
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
    API_BASE_URL: 'http://localhost',
    authHeaders: vi.fn(async () => ({})),
    authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  });
});

vi.mock('./AutonomousStatus', () => ({
  AutonomousStatus: () => null,
}));

describe('PrimaryTabNav — cross-tab alert indicator (PR #15230)', () => {
  let container: HTMLDivElement;
  let root: Root;

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function render(ui: React.ReactElement) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(ui));
    return container;
  }

  test('shows alert dot on all four tab buttons when activeAlerts > 0', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={3}
      />,
    );
    act(() => {});

    const buttons = el.querySelectorAll<HTMLButtonElement>('button');
    expect(buttons.length).toBe(4);
    // Every tab button should contain an alert-dot span regardless of which tab is active
    let dotCount = 0;
    for (const button of buttons) {
      const dot = button.querySelector('[title*="active alert"]');
      expect(dot).not.toBeNull();
      dotCount++;
    }
    expect(dotCount).toBe(4);
  });

  test('shows no alert dot when activeAlerts is 0', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={0}
      />,
    );
    act(() => {});

    const dots = el.querySelectorAll('[title*="active alert"]');
    expect(dots.length).toBe(0);
  });

  test('shows no alert dot when activeAlerts is omitted (defaults to 0)', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
      />,
    );
    act(() => {});

    const dots = el.querySelectorAll('[title*="active alert"]');
    expect(dots.length).toBe(0);
  });

  test('dot title is singular when activeAlerts === 1', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={1}
      />,
    );
    act(() => {});

    // Exactly "1 active alert" (not "1 active alerts")
    const singularDots = el.querySelectorAll('[title="1 active alert"]');
    expect(singularDots.length).toBeGreaterThan(0);

    const pluralWrongDots = el.querySelectorAll('[title="1 active alerts"]');
    expect(pluralWrongDots.length).toBe(0);
  });

  test('dot title is plural when activeAlerts > 1', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="pipeline"
        onTabChange={() => {}}
        activeAlerts={5}
      />,
    );
    act(() => {});

    const dots = el.querySelectorAll('[title="5 active alerts"]');
    expect(dots.length).toBeGreaterThan(0);
  });

  test('alert indicator is visible on pipeline tab when active tab is chat (cross-tab)', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={2}
      />,
    );
    act(() => {});

    const pipelineBtn = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(b => b.textContent?.includes('Pipeline'));
    expect(pipelineBtn).toBeDefined();
    const pipelineDot = pipelineBtn?.querySelector('[title*="active alert"]');
    expect(pipelineDot).not.toBeNull();
  });

  test('alert indicator is visible on project tab when active tab is chat (cross-tab)', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={2}
      />,
    );
    act(() => {});

    const projectBtn = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(b => b.textContent?.includes('Project'));
    expect(projectBtn).toBeDefined();
    const projectDot = projectBtn?.querySelector('[title*="active alert"]');
    expect(projectDot).not.toBeNull();
  });

  test('alert indicator is visible on runtime tab when active tab is chat (cross-tab)', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={2}
      />,
    );
    act(() => {});

    const runtimeBtn = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(b => b.textContent?.includes('Runtime'));
    expect(runtimeBtn).toBeDefined();
    const runtimeDot = runtimeBtn?.querySelector('[title*="active alert"]');
    expect(runtimeDot).not.toBeNull();
  });

  test('alert indicator is visible on chat tab when active tab is pipeline (cross-tab)', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="pipeline"
        onTabChange={() => {}}
        activeAlerts={1}
      />,
    );
    act(() => {});

    const chatBtn = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(b => b.textContent?.includes('Chat'));
    expect(chatBtn).toBeDefined();
    const chatDot = chatBtn?.querySelector('[title*="active alert"]');
    expect(chatDot).not.toBeNull();
  });

  test('alert dot coexists with chat badge count', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="pipeline"
        onTabChange={() => {}}
        openMessageCount={7}
        activeAlerts={2}
      />,
    );
    act(() => {});

    const chatBtn = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(b => b.textContent?.includes('Chat'));
    expect(chatBtn?.textContent).toContain('7');
    // Chat tab should also carry the alert dot even though it has a badge
    const chatDot = chatBtn?.querySelector('[title*="active alert"]');
    expect(chatDot).not.toBeNull();
  });

  test('alert dot coexists with pipeline attention badge count', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        needsAttentionCount={4}
        activeAlerts={1}
      />,
    );
    act(() => {});

    const pipelineBtn = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(b => b.textContent?.includes('Pipeline'));
    expect(pipelineBtn?.textContent).toContain('4');
    const pipelineDot = pipelineBtn?.querySelector('[title*="active alert"]');
    expect(pipelineDot).not.toBeNull();
  });

  test('alert dot uses red circular styling (bg-red-500)', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={1}
      />,
    );
    act(() => {});

    const redDots = el.querySelectorAll('.bg-red-500');
    expect(redDots.length).toBeGreaterThan(0);
  });

  test('no bg-red-500 elements rendered when activeAlerts is 0', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={0}
      />,
    );
    act(() => {});

    const redDots = el.querySelectorAll('.bg-red-500');
    expect(redDots.length).toBe(0);
  });

  test('tab click handler fires for all tabs regardless of alert state', () => {
    const onTabChange = vi.fn();
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={onTabChange}
        activeAlerts={3}
      />,
    );
    act(() => {});

    const buttons = el.querySelectorAll<HTMLButtonElement>('button');
    act(() => { buttons[1].click(); }); // pipeline
    expect(onTabChange).toHaveBeenCalledWith('pipeline');

    act(() => { buttons[2].click(); }); // project
    expect(onTabChange).toHaveBeenCalledWith('project');
  });

  test('renders all four tabs (chat, pipeline, project, runtime)', () => {
    const el = render(
      <PrimaryTabNav
        activeTab="chat"
        onTabChange={() => {}}
        activeAlerts={1}
      />,
    );
    act(() => {});

    const buttons = Array.from(el.querySelectorAll<HTMLButtonElement>('button'));
    const labels = buttons.map(b => b.textContent?.replace(/\d+/g, '').trim());
    expect(labels).toContain('Chat');
    expect(labels).toContain('Pipeline');
    expect(labels).toContain('Project');
    expect(labels).toContain('Runtime');
  });
});
