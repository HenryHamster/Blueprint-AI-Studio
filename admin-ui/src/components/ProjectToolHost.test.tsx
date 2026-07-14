import { afterEach, beforeAll, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectToolHost } from './ProjectToolHost';
import { registerToolKind } from '../toolRegistry';
import type { ToolComponentProps } from '../toolRegistry';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchProjectTools: vi.fn(),
    fetchProjectToolManifest: vi.fn(),
    fetchToolKinds: vi.fn(),
    updateProjectTool: vi.fn(),
    seedProjectTools: vi.fn(),
    fetchAssetToken: vi.fn(),
  };
});

import {
  fetchProjectTools,
  fetchProjectToolManifest,
  fetchToolKinds,
  updateProjectTool,
  fetchAssetToken,
  type FetchProjectToolsResult,
  type ProjectTool,
  type ProjectToolManifestEntry,
  type ToolKindConfig,
} from '../api/projectTools';
import { loadHiddenManifestSlugs } from './projectToolHidden';

function MockWidget({ tool }: ToolComponentProps) {
  return <div className="p-2 text-xs">{tool.title} content</div>;
}

beforeAll(() => {
  registerToolKind('mock-widget', MockWidget);
});

const TOOL: ProjectTool = {
  id: 'tool-1',
  kind: 'mock-widget',
  slug: 'mock-widget',
  title: 'My Widget',
  enabled: true,
  placement: 'project_section',
  content_revision: 0,
  config: {},
};

const RESULT: FetchProjectToolsResult = { tools: [TOOL], total_count: 1, next_offset: null };

const KINDS: ToolKindConfig[] = [
  {
    kind: 'mock-widget',
    label: 'Mock Widget',
    description: 'a mock',
    config_schema: { properties: { threshold: { type: 'integer' } } },
  },
];

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function mount(node: React.ReactElement) {
  host = document.createElement('div');
  document.body.appendChild(host);
  const r = createRoot(host);
  root = r;
  await act(async () => { r.render(node); });
  await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
}

function h(): HTMLDivElement {
  if (!host) throw new Error('host not mounted');
  return host;
}

function findButton(label: string): HTMLButtonElement | undefined {
  return Array.from(h().querySelectorAll('button')).find(b => b.textContent === label);
}

function requireButton(label: string): HTMLButtonElement {
  const btn = findButton(label);
  if (!btn) throw new Error(`button not found: ${label}`);
  return btn;
}

// React 18 + happy-dom pattern: set value through the native prototype setter
// so React's onChange fires with the new value.
function setInputValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

function setSelectValue(el: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value')?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

describe('ProjectToolHost — configure / rename / role gating', () => {
  beforeEach(() => {
    vi.mocked(fetchProjectTools).mockResolvedValue(RESULT);
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({ tools: [], skills: [], error: null });
    vi.mocked(fetchToolKinds).mockResolvedValue(KINDS);
    vi.mocked(updateProjectTool).mockResolvedValue({ ...TOOL });
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.clearAllMocks();
  });

  test('owner sees management + Configure controls', async () => {
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    expect(findButton('+ Add tool')).toBeTruthy();
    expect(h().textContent).toContain('Seed defaults');
    expect(findButton('Configure')).toBeTruthy();
    expect(findButton('Disable')).toBeTruthy();
    expect(findButton('Remove')).toBeTruthy();
  });

  test('member sees no owner-only management controls', async () => {
    await mount(<ProjectToolHost projectId="proj-a" myRole="member" />);
    // Tool content still renders for members.
    expect(h().textContent).toContain('My Widget content');
    // But none of the owner-only buttons are present.
    expect(findButton('+ Add tool')).toBeUndefined();
    expect(h().textContent).not.toContain('Seed defaults');
    expect(findButton('Configure')).toBeUndefined();
    expect(findButton('Disable')).toBeUndefined();
    expect(findButton('Remove')).toBeUndefined();
  });

  test('undefined role is treated as owner (backend still guards)', async () => {
    await mount(<ProjectToolHost projectId="proj-a" />);
    expect(findButton('+ Add tool')).toBeTruthy();
    expect(findButton('Configure')).toBeTruthy();
  });

  test('rename persists the new title via updateProjectTool', async () => {
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    await act(async () => { requireButton('Configure').click(); });
    // wait for tool kinds to load
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    const titleInput = h().querySelector('input[type="text"]') as HTMLInputElement;
    expect(titleInput.value).toBe('My Widget');
    await act(async () => { setInputValue(titleInput, 'Renamed Widget'); });
    await act(async () => { requireButton('Save').click(); });
    expect(vi.mocked(updateProjectTool)).toHaveBeenCalledWith(
      'proj-a',
      'tool-1',
      expect.objectContaining({ title: 'Renamed Widget', placement: 'project_section' }),
    );
  });

  test('configure sends placement + coerced schema config', async () => {
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    await act(async () => { requireButton('Configure').click(); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    // Placement select + one schema field (threshold:integer) are rendered.
    const select = h().querySelector('select') as HTMLSelectElement;
    expect(Array.from(select.options).map(o => o.value)).toEqual([
      'project_section', 'toolbar', 'sidebar',
    ]);
    await act(async () => { setSelectValue(select, 'sidebar'); });
    // The second text input is the `threshold` config field.
    const inputs = h().querySelectorAll('input[type="text"]');
    const thresholdInput = inputs[1] as HTMLInputElement;
    await act(async () => { setInputValue(thresholdInput, '42'); });
    await act(async () => { requireButton('Save').click(); });
    expect(vi.mocked(updateProjectTool)).toHaveBeenCalledWith(
      'proj-a',
      'tool-1',
      { title: 'My Widget', placement: 'sidebar', config: { threshold: 42 } },
    );
  });

  test('empty title blocks save and shows an error', async () => {
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    await act(async () => { requireButton('Configure').click(); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    const titleInput = h().querySelector('input[type="text"]') as HTMLInputElement;
    await act(async () => { setInputValue(titleInput, '   '); });
    // Save button is disabled for blank titles.
    expect((findButton('Save') as HTMLButtonElement).disabled).toBe(true);
    expect(vi.mocked(updateProjectTool)).not.toHaveBeenCalled();
  });
});

const MANIFEST_TOOL: ProjectToolManifestEntry = {
  slug: 'custom-viewer',
  title: 'Custom Viewer',
  description: 'A project-declared viewer',
  placement: 'project_section',
  entry: 'index.html',
  kind: 'project',
  source: 'project',
};

describe('ProjectToolHost — manifest tool Hide persistence', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(fetchProjectTools).mockResolvedValue({ tools: [], total_count: 0, next_offset: null });
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({ tools: [MANIFEST_TOOL], skills: [], error: null });
    vi.mocked(fetchToolKinds).mockResolvedValue(KINDS);
    // The iframe mints an asset token on mount; resolve it so no unhandled
    // rejection leaks into the test (the Hide button lives outside the iframe).
    vi.mocked(fetchAssetToken).mockResolvedValue({ token: 't', expires_at: '2026-07-11T12:00:00Z' });
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
  });

  test('Hide persists the slug to localStorage and hides the tool', async () => {
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    expect(h().textContent).toContain('Custom Viewer');

    await act(async () => { requireButton('Hide').click(); });

    // Tool is gone from the visible grid...
    expect(findButton('Hide')).toBeUndefined();
    // ...persisted so a reload keeps it hidden...
    expect([...loadHiddenManifestSlugs('proj-a')]).toEqual(['custom-viewer']);
    // ...and the restore affordance is reachable.
    expect(findButton('Show hidden')).toBeTruthy();
  });

  test('a persisted hidden slug is respected on mount', async () => {
    localStorage.setItem('project_tools_hidden:proj-a', JSON.stringify(['custom-viewer']));
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    // The tool starts hidden; only the restore affordance shows.
    expect(findButton('Hide')).toBeUndefined();
    expect(findButton('Show hidden')).toBeTruthy();
  });

  test('Show hidden restores the tool and clears storage', async () => {
    localStorage.setItem('project_tools_hidden:proj-a', JSON.stringify(['custom-viewer']));
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    await act(async () => { requireButton('Show hidden').click(); });
    expect(findButton('Hide')).toBeTruthy();
    expect(loadHiddenManifestSlugs('proj-a').size).toBe(0);
  });
});
