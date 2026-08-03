import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { HeaderBar } from './HeaderBar';
import type { Project } from '../types/project';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
    API_BASE_URL: 'http://localhost',
    authHeaders: vi.fn(async () => ({})),
    authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  });
});

const mockProjects: Project[] = [
  {
    id: 'proj-1',
    name: 'Project 1',
    status: 'active',
    github_owner: 'owner1',
    github_repo: 'repo1',
    autonomous_enabled: false,
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 'proj-2',
    name: 'Project 2',
    status: 'active',
    github_owner: 'owner2',
    github_repo: 'repo2',
    autonomous_enabled: false,
    created_at: '2024-01-01T00:00:00Z',
  },
];

function makeProps(overrides: Record<string, unknown> = {}) {
  return {
    tasks: [],
    onRefresh: vi.fn(),
    showResearchBrowser: false,
    onToggleResearchBrowser: vi.fn(),
    showKnowledgeBrowser: false,
    onToggleKnowledgeBrowser: vi.fn(),
    openMessageCount: 0,
    onOpenNewTaskModal: vi.fn(),
    onOpenSettings: vi.fn(),
    canManagePlatform: true,
    onSignOut: vi.fn(),
    userEmail: 'test@example.com',
    wsRef: { current: null } as React.RefObject<WebSocket | null>,
    apiHealthy: true,
    wsConnected: true,
    lastFetchTime: Date.now(),
    deployState: null,
    selectedProjectId: 'proj-1',
    projects: mockProjects,
    onSelectProject: vi.fn(),
    activeTab: 'pipeline' as const,
    onTabChange: vi.fn(),
    health: null,
    detailedHealth: null,
    ...overrides,
  };
}

let container: HTMLDivElement;
let root: Root;

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

function render(ui: React.ReactElement) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(ui));
  return container;
}

describe('HeaderBar ceiling-deferred indicator', () => {
  test('renders ceiling-deferred badge when selected project is ceiling-deferred', () => {
    const el = render(
      <HeaderBar
        {...makeProps({
          ceilingDeferredProjectIds: new Set(['proj-1']),
        })}
      />,
    );
    act(() => {});
    expect(el.textContent).toContain('ceiling');
    expect(el.textContent).toContain('⚠');
  });

  test('does not render ceiling-deferred badge when selected project is not ceiling-deferred', () => {
    const el = render(
      <HeaderBar
        {...makeProps({
          ceilingDeferredProjectIds: new Set(['proj-2']),
        })}
      />,
    );
    act(() => {});
    expect(el.textContent).not.toContain('ceiling');
  });

  test('does not render ceiling-deferred badge when no projects are ceiling-deferred', () => {
    const el = render(
      <HeaderBar
        {...makeProps({
          ceilingDeferredProjectIds: new Set(),
        })}
      />,
    );
    act(() => {});
    expect(el.textContent).not.toContain('ceiling');
  });

  test('does not render ceiling-deferred badge when ceilingDeferredProjectIds is undefined', () => {
    const el = render(<HeaderBar {...makeProps({ ceilingDeferredProjectIds: undefined })} />);
    act(() => {});
    expect(el.textContent).not.toContain('ceiling');
  });
});

describe('HeaderBar platform access', () => {
  test('hides Runtime settings when platform-management access is unavailable', () => {
    const el = render(<HeaderBar {...makeProps({ canManagePlatform: false })} />);
    expect(el.querySelector('button[title="Runtime settings"]')).toBeNull();
    expect(el.textContent).not.toContain('Runtime settings');
  });
});
