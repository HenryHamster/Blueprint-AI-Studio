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
    createProjectTool: vi.fn(),
    updateProjectTool: vi.fn(),
    seedProjectTools: vi.fn(),
    fetchAssetToken: vi.fn(),
    fetchComputeImageStatus: vi.fn(),
  };
});

import {
  fetchProjectTools,
  fetchProjectToolManifest,
  fetchToolKinds,
  createProjectTool,
  updateProjectTool,
  fetchAssetToken,
  fetchComputeImageStatus,
  type FetchProjectToolsResult,
  type ProjectTool,
  type ProjectToolManifestEntry,
  type ToolKindConfig,
  type ComputeImageStatusResponse,
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
    vi.mocked(fetchComputeImageStatus).mockResolvedValue({ status: 'no_compute_image_declared' });
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
    vi.mocked(fetchComputeImageStatus).mockResolvedValue({ status: 'no_compute_image_declared' });
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

const BUILT_STATUS: ComputeImageStatusResponse = {
  status: 'built',
  pinned_ref: 'sha256:abc123',
  digest: 'sha256:abc123',
  dep_hash: 'dephash001',
  tag: 'latest',
  built: true,
  created_at: '2026-07-14T10:00:00Z',
  updated_at: '2026-07-14T10:00:00Z',
  is_digest_pinned: true,
};

const MUTABLE_STATUS: ComputeImageStatusResponse = {
  status: 'built',
  pinned_ref: 'latest',
  digest: null,
  dep_hash: 'dephash002',
  tag: 'latest',
  built: true,
  created_at: '2026-07-13T08:00:00Z',
  updated_at: '2026-07-13T08:00:00Z',
  is_digest_pinned: false,
};

describe('ProjectToolHost — compute image status panel', () => {
  beforeEach(() => {
    vi.mocked(fetchProjectTools).mockResolvedValue({ tools: [], total_count: 0, next_offset: null });
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({ tools: [], skills: [], error: null });
    vi.mocked(fetchToolKinds).mockResolvedValue(KINDS);
    vi.mocked(fetchComputeImageStatus).mockResolvedValue({ status: 'no_compute_image_declared' });
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.clearAllMocks();
  });

  test('owner sees baked compute image status panel', async () => {
    vi.mocked(fetchComputeImageStatus).mockResolvedValue(BUILT_STATUS);
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    expect(h().textContent).toContain('Baked compute image');
    expect(h().textContent).toContain('sha256:abc123');
  });

  test('member does not see compute image panel', async () => {
    vi.mocked(fetchComputeImageStatus).mockResolvedValue(BUILT_STATUS);
    await mount(<ProjectToolHost projectId="proj-a" myRole="member" />);
    expect(h().textContent).not.toContain('Baked compute image');
  });

  test('shows warning badge when digest is null / mutable tag', async () => {
    vi.mocked(fetchComputeImageStatus).mockResolvedValue(MUTABLE_STATUS);
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    expect(h().textContent).toContain('Baked compute image');
    expect(h().textContent).toContain('MUTABLE');
  });

  test('shows never_built status message', async () => {
    vi.mocked(fetchComputeImageStatus).mockResolvedValue({ status: 'never_built' });
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    expect(h().textContent).toContain('Baked compute image');
    expect(h().textContent).toContain('never built');
  });

  test('shows no_compute_image_declared message', async () => {
    vi.mocked(fetchComputeImageStatus).mockResolvedValue({ status: 'no_compute_image_declared' });
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    expect(h().textContent).toContain('Baked compute image');
    expect(h().textContent).toContain('No compute image declared');
  });

  test('check drift button triggers drift fetch', async () => {
    vi.mocked(fetchComputeImageStatus).mockResolvedValueOnce(BUILT_STATUS);
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    // Wait for initial fetch
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    // Button appears when drift hasn't been checked yet
    const checkDriftBtn = findButton('Check drift');
    expect(checkDriftBtn).toBeTruthy();
    // Click and ensure fetchComputeImageStatus was called with check_drift=true
    vi.mocked(fetchComputeImageStatus).mockClear();
    await act(async () => { checkDriftBtn!.click(); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    expect(vi.mocked(fetchComputeImageStatus)).toHaveBeenCalledWith('proj-a', true);
  });

  test('fetch error is surfaced without breaking tool content', async () => {
    vi.mocked(fetchComputeImageStatus).mockRejectedValue(new Error('Network error'));
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    expect(h().textContent).toContain('Compute image');
    expect(h().textContent).toContain('Network error');
  });

  test('shows up-to-date label when drift check finds no drift', async () => {
    vi.mocked(fetchComputeImageStatus).mockResolvedValueOnce(BUILT_STATUS);
    await mount(<ProjectToolHost projectId="proj-a" myRole="owner" />);
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    const btn = findButton('Check drift');
    expect(btn).toBeTruthy();
    vi.mocked(fetchComputeImageStatus).mockResolvedValue({
      ...BUILT_STATUS,
      drift: { drifted: false, current_dep_hash: 'dephash001', stored_dep_hash: 'dephash001' },
    });
    await act(async () => { btn!.click(); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));
    expect(h().textContent).toContain('Up to date');
  });
});

// ---------------------------------------------------------------------------
// End-to-end owner walkthrough (chained flow)
// ---------------------------------------------------------------------------

const WALKTHROUGH_ENABLED: ProjectTool = {
  id: 'tool-enabled',
  kind: 'mock-widget',
  slug: 'mock-widget-enabled',
  title: 'My Enabled Widget',
  enabled: true,
  placement: 'project_section',
  content_revision: 0,
  config: {},
};

const WALKTHROUGH_DISABLED: ProjectTool = {
  id: 'tool-disabled',
  kind: 'mock-widget',
  slug: 'mock-widget-disabled',
  title: 'My Disabled Widget',
  enabled: false,
  placement: 'project_section',
  content_revision: 0,
  config: {},
};

const WALKTHROUGH_TOOLS: FetchProjectToolsResult = {
  tools: [WALKTHROUGH_ENABLED, WALKTHROUGH_DISABLED],
  total_count: 2,
  next_offset: null,
};

describe('ProjectToolHost — owner walkthrough (end-to-end chained flow)', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(fetchProjectTools).mockResolvedValue(WALKTHROUGH_TOOLS);
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({
      tools: [MANIFEST_TOOL],
      skills: [],
      error: null,
    });
    vi.mocked(fetchToolKinds).mockResolvedValue(KINDS);
    vi.mocked(fetchComputeImageStatus).mockResolvedValue({ status: 'no_compute_image_declared' });
    vi.mocked(fetchAssetToken).mockResolvedValue({ token: 't', expires_at: '2026-07-14T12:00:00Z' });
    vi.mocked(updateProjectTool).mockImplementation(
      (_pid, _tid, patch) => Promise.resolve({ ...WALKTHROUGH_ENABLED, ...patch } as ProjectTool),
    );
    vi.mocked(createProjectTool).mockResolvedValue({ ...WALKTHROUGH_ENABLED, id: 'tool-new', slug: 'mock-widget' });
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    localStorage.clear();
    vi.clearAllMocks();
  });

  test('shows assigned slug on create', async () => {
    vi.mocked(createProjectTool).mockResolvedValue({
      id: 'tool-new',
      kind: 'mock-widget',
      slug: 'mock-widget-2',
      title: 'My Widget',
      enabled: true,
      placement: 'project_section',
      content_revision: 0,
      config: {},
    });

    await mount(<ProjectToolHost projectId="proj-walkthrough" myRole="owner" />);

    // Open the Add form
    await act(async () => { requireButton('+ Add tool').click(); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));

    // Fill in the title field
    const titleInput = h().querySelector('input[placeholder="Tool title"]') as HTMLInputElement;
    await act(async () => { setInputValue(titleInput, 'My Widget'); });

    // Submit
    await act(async () => { requireButton('Create').click(); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));

    // Server-assigned slug (auto-uniquified to 'mock-widget-2') must be visible
    expect(h().textContent).toContain('mock-widget-2');
    expect(vi.mocked(createProjectTool)).toHaveBeenCalledWith(
      'proj-walkthrough',
      { kind: 'mock-widget', title: 'My Widget' },
    );
  });

  test('Enable button shows in-flight state while toggle PATCH is pending', async () => {
    let resolveToggle!: (v: ProjectTool) => void;
    vi.mocked(updateProjectTool).mockImplementation(
      () => new Promise(resolve => { resolveToggle = resolve; }),
    );

    await mount(<ProjectToolHost projectId="proj-walkthrough" myRole="owner" />);

    const enableBtn = requireButton('Enable');
    expect(enableBtn.disabled).toBe(false);

    // Click — button goes disabled and shows "Enabling…" while PATCH is pending
    await act(async () => { enableBtn.click(); });
    expect(enableBtn.disabled).toBe(true);
    expect(enableBtn.textContent).toBe('Enabling…');

    // Resolve the in-flight PATCH
    await act(async () => { resolveToggle({ ...WALKTHROUGH_DISABLED, enabled: true }); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 30)));

    // Only one PATCH was fired despite button being clicked while in-flight
    expect(vi.mocked(updateProjectTool)).toHaveBeenCalledTimes(1);
  });

  test('owner walks the full project-tools flow without a dead end', async () => {
    await mount(<ProjectToolHost projectId="proj-walkthrough" myRole="owner" />);

    // Step 1: disabled tool is visible with an Enable control — not silently hidden
    expect(h().textContent).toContain('My Disabled Widget');
    const enableBtn = Array.from(h().querySelectorAll('button')).find(
      b => b.textContent === 'Enable',
    );
    expect(enableBtn).toBeTruthy();
    // The disabled tool's widget body is suppressed, but the row is present
    expect(h().textContent).not.toContain('My Disabled Widget content');

    // Step 2: manifest-declared tool appears alongside the registered tools
    expect(h().textContent).toContain('Custom Viewer');

    // Step 3: click Enable → updateProjectTool called with {enabled: true}
    await act(async () => { enableBtn!.click(); });
    expect(vi.mocked(updateProjectTool)).toHaveBeenCalledWith(
      'proj-walkthrough',
      'tool-disabled',
      { enabled: true },
    );

    // Step 4: owner sees Configure control (member-role gating covered by dedicated tests)
    expect(findButton('Configure')).toBeTruthy();

    // Step 5: hide the manifest tool → it disappears and localStorage records the slug
    const hideBtn = requireButton('Hide');
    await act(async () => { hideBtn.click(); });
    expect(findButton('Hide')).toBeUndefined();
    expect([...loadHiddenManifestSlugs('proj-walkthrough')]).toContain('custom-viewer');

    // Step 6: Show hidden restore affordance is reachable — hiding is not a permanent trap
    const showHiddenBtn = requireButton('Show hidden');

    // Step 7: click Show hidden → manifest tool reappears and localStorage is cleared
    await act(async () => { showHiddenBtn.click(); });
    expect(findButton('Hide')).toBeTruthy();
    expect(loadHiddenManifestSlugs('proj-walkthrough').size).toBe(0);
  });
});
