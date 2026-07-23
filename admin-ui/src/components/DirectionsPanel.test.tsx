import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DirectionsPanel } from './DirectionsPanel';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***' }),
    // DirectionsPanel calls apiFetch, not fetch directly — route through
    // the global fetch stub so the test's mockFetch() responses still apply.
    apiFetch: (path: string, init?: RequestInit) => (globalThis.fetch as typeof fetch)(path, init),
    // useDirectionsData also imports fetchVersionLifecycle and (since the #11917
    // perf reorder) calls it at the TOP of fetchDirections, before the directions
    // apiFetch calls. The mock factory fully replaces '../api', so this export must
    // be present or it resolves to undefined and the call throws synchronously —
    // which the try/catch swallows, silently skipping every later apiFetch.
    fetchVersionLifecycle: vi.fn().mockResolvedValue(null),
  });
});

vi.mock('./DirectionCard', () => ({
  DirectionCard: ({ direction }: { direction: { text: string } }) => (
    <div data-testid="direction-card">{direction.text}</div>
  ),
}));

vi.mock('./DesignDocViewer', () => ({
  default: () => <div data-testid="design-doc-viewer">DesignDoc</div>,
}));

vi.mock('./ProblemModelViewer', () => ({
  default: () => <div data-testid="problem-model-viewer">ProblemModelContent</div>,
}));

vi.mock('./RunningUpdatePanel', () => ({
  default: () => <div data-testid="running-update-panel">RunningUpdateContent</div>,
}));

vi.mock('./AutonomousConfigSection', () => ({
  AutonomousConfigSection: () => <div>AutonomousConfig</div>,
}));

// Stable form mock with a persistent addIdempotencyKeyRef so retries observed
// by the panel reuse one key (resetAddForm clears it, mirroring the real hook).
const formMock = vi.hoisted(() => {
  const addIdempotencyKeyRef = { current: null as string | null };
  return {
    newText: 'Test direction',
    setNewText: vi.fn(),
    newPriority: 'medium',
    setNewPriority: vi.fn(),
    newCategory: null,
    setNewCategory: vi.fn(),
    newPathTags: [],
    setNewPathTags: vi.fn(),
    newPathInput: '',
    setNewPathInput: vi.fn(),
    newCompletionCriteria: 'Done when merged',
    setNewCompletionCriteria: vi.fn(),
    newStaged: false,
    setNewStaged: vi.fn(),
    addIdempotencyKeyRef,
    markAddAttemptFailed: vi.fn(),
    newSuccessEvidence: 'Latency p99 < 200ms in prod',
    setNewSuccessEvidence: vi.fn(),
    newRetirementCondition: 'One clean promotion cycle',
    setNewRetirementCondition: vi.fn(),
    editingId: null,
    setEditingId: vi.fn(),
    editText: '',
    editPriority: 'medium',
    editStatus: 'active',
    editCategory: null,
    setEditCategory: vi.fn(),
    editPathTags: [],
    editPathInput: '',
    setEditText: vi.fn(),
    setEditPriority: vi.fn(),
    setEditStatus: vi.fn(),
    setEditPathTags: vi.fn(),
    setEditPathInput: vi.fn(),
    editCompletionCriteria: '',
    setEditCompletionCriteria: vi.fn(),
    editSuccessEvidence: '',
    setEditSuccessEvidence: vi.fn(),
    editRetirementCondition: '',
    setEditRetirementCondition: vi.fn(),
    startEdit: vi.fn(),
    resetAddForm: vi.fn(() => {
      addIdempotencyKeyRef.current = null;
    }),
  };
});

vi.mock('../hooks/useDirectionForm', () => ({
  useDirectionForm: () => formMock,
}));

vi.mock('../hooks/useAutonomousConfig', () => ({
  useAutonomousConfig: () => ({
    fetchCooldownSetting: vi.fn(),
    fetchSettings: vi.fn(),
    configCooldownHours: 24,
    setConfigCooldownHours: vi.fn(),
    configIntervalMinutes: 60,
    setConfigIntervalMinutes: vi.fn(),
    configMaxParallel: 3,
    setConfigMaxParallel: vi.fn(),
    configMaxTotalResearch: 5,
    setConfigMaxTotalResearch: vi.fn(),
    configMaxTasksPerHour: 10,
    setConfigMaxTasksPerHour: vi.fn(),
    autonomousConfigOpen: false,
    setAutonomousConfigOpen: vi.fn(),
    savingConfigField: null,
    saveConfigField: vi.fn(),
    cooldownMinutes: 240,
  }),
}));

let fetchSpy: ReturnType<typeof vi.fn>;

function mockFetch(responses: Record<string, unknown>) {
  fetchSpy.mockImplementation((url: RequestInfo | URL) => {
    const urlStr = url.toString();
    // Sort by key length descending so more-specific paths match first
    const entries = Object.entries(responses).sort((a, b) => b[0].length - a[0].length);
    const match = entries.find(([k]) => urlStr.includes(k));
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(match ? match[1] : {}),
    } as Response);
  });
}

const TEST_PROJECTS = [{ id: 'proj1', name: 'Project 1', status: 'active', github_owner: 'test', github_repo: 'test-repo', autonomous_enabled: false, created_at: '2024-01-01T00:00:00Z' }];

const baseResponses = {
  '/api/admin/projects/proj1/directions/task-counts': { counts: {} },
  '/api/admin/projects/proj1/directions': {
    directions: [],
    autonomous_enabled: false,
    vision: {},
    design_doc: {},
  },
};

describe('DirectionsPanel', () => {
  const originalError = console.error;

  beforeEach(() => {
    console.error = vi.fn();
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    formMock.addIdempotencyKeyRef.current = null;
  });

  afterEach(() => {
    console.error = originalError;
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('renders directions for the passed selectedProjectId and has no project picker', async () => {
    mockFetch(baseResponses);
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByRole('button', { name: 'New direction' });
    // No project <select> dropdown should be rendered
    expect(screen.queryByRole('combobox', { name: /project/i })).toBeNull();
    // Only the priority and category selects are present, not a project picker
    const selects = document.querySelectorAll('select');
    // project options should not appear
    expect(Array.from(selects).some(s => s.innerHTML.includes('Project 1'))).toBe(false);
  });

  it('refetches directions when selectedProjectId prop changes', async () => {
    mockFetch({
      ...baseResponses,
      '/api/admin/projects/proj2/directions/task-counts': { counts: {} },
      '/api/admin/projects/proj2/directions': {
        directions: [{ id: 'd2', text: 'Project 2 direction', priority: 'low', status: 'active', added_at: '2024-01-01T00:00:00Z' }],
        autonomous_enabled: false,
        vision: {},
        design_doc: {},
      },
    });
    const { rerender } = render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByRole('button', { name: 'New direction' });
    const proj1CallCount = fetchSpy.mock.calls.filter((c) => String(c[0]).includes('proj1')).length;
    expect(proj1CallCount).toBeGreaterThan(0);

    rerender(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj2" projects={TEST_PROJECTS} />);
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some((c) => String(c[0]).includes('proj2'))).toBe(true);
    });
  });

  it('fetches directions for the selected project and renders direction cards', async () => {
    mockFetch({
      ...baseResponses,
      '/api/admin/projects/proj1/directions': {
        directions: [
          {
            id: 'd1',
            text: 'Build the feature',
            priority: 'high',
            status: 'active',
            added_at: '2024-01-01T00:00:00Z',
          },
        ],
        autonomous_enabled: false,
        vision: {},
        design_doc: {},
      },
    });
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByText('Build the feature');
    expect(screen.getByTestId('direction-card')).toBeInTheDocument();
  });

  it('shows status section labels for grouped directions', async () => {
    mockFetch({
      ...baseResponses,
      '/api/admin/projects/proj1/directions': {
        directions: [
          { id: 'd1', text: 'Active direction', priority: 'high', status: 'active', added_at: '2024-01-01T00:00:00Z' },
          { id: 'd2', text: 'Proposed direction', priority: 'medium', status: 'proposed', added_at: '2024-01-01T00:00:00Z' },
        ],
        autonomous_enabled: false,
        vision: {},
        design_doc: {},
      },
    });
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByText('Active direction');
    // Section headers render as "{label} ({count})"
    expect(screen.getAllByText('Active (1)').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Proposed by Agent (1)').length).toBeGreaterThan(0);
  });

  it('clicking the Problem Model tab shows ProblemModelViewer', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByRole('button', { name: 'New direction' });
    await user.click(screen.getByRole('button', { name: /Problem Model/ }));
    expect(screen.getByTestId('problem-model-viewer')).toBeInTheDocument();
  });

  it('clicking the Running Update tab shows RunningUpdatePanel', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByRole('button', { name: 'New direction' });
    await user.click(screen.getByRole('button', { name: /Running Update/ }));
    expect(screen.getByTestId('running-update-panel')).toBeInTheDocument();
  });

  it('clicking the Design Doc tab shows DesignDocViewer', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByRole('button', { name: 'New direction' });
    await user.click(screen.getByRole('button', { name: 'Design Doc' }));
    expect(screen.getByTestId('design-doc-viewer')).toBeInTheDocument();
  });

  it('clicking the Add button triggers a POST fetch for the direction', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    // Expand the add-direction form, then locate the input
    await user.click(await screen.findByRole('button', { name: 'New direction' }));
    const input = await screen.findByPlaceholderText('New direction...');
    // Add button is the only button in the same flex row as the input
    const parent = input.parentElement;
    if (!parent) throw new Error('test setup: expected input parent');
    const addBtn = parent.querySelector('button');
    if (!addBtn) throw new Error('test setup: expected add button');
    await user.click(addBtn);
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        expect.stringContaining('/directions'),
        expect.objectContaining({ method: 'POST' })
      );
    });
    const postCall = fetchSpy.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    );
    expect(postCall).toBeDefined();
    if (!postCall) throw new Error('test setup: expected POST call');
    const body = JSON.parse((postCall[1] as RequestInit).body as string);
    expect(body.completion_criteria).toBe('Done when merged');
    expect(body.success_evidence).toBe('Latency p99 < 200ms in prod');
    expect(body.retirement_condition).toBe('One clean promotion cycle');
    // Every create now carries a client idempotency identity.
    expect(typeof body.idempotency_key).toBe('string');
    expect(body.idempotency_key.length).toBeGreaterThan(0);
  });

  it('reuses one idempotency_key when a failed add is retried', async () => {
    const user = userEvent.setup();
    // First POST fails (non-2xx), every other route is OK.
    fetchSpy.mockImplementation((url: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = url.toString();
      const isAddPost =
        init?.method === 'POST' && urlStr.includes('/directions') && !urlStr.includes('task-counts');
      if (isAddPost && !(fetchSpy as unknown as { _added?: boolean })._added) {
        (fetchSpy as unknown as { _added?: boolean })._added = true;
        return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) } as Response);
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ directions: [] }) } as Response);
    });

    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);

    // First attempt — expand, click add (fails), form collapses
    await user.click(await screen.findByRole('button', { name: 'New direction' }));
    const input = await screen.findByPlaceholderText('New direction...');
    const addBtn = input.parentElement?.querySelector('button');
    if (!addBtn) throw new Error('test setup: expected add button');
    await user.click(addBtn); // fails — key retained, form collapses

    // Retry — re-expand form, find a fresh add button reference
    await user.click(await screen.findByRole('button', { name: 'New direction' }));
    const input2 = await screen.findByPlaceholderText('New direction...');
    const addBtn2 = input2.parentElement?.querySelector('button');
    if (!addBtn2) throw new Error('test setup: expected add button on retry');
    await user.click(addBtn2); // retry — same key

    await waitFor(() => {
      const posts = fetchSpy.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST' && c[0].toString().includes('/directions') && !c[0].toString().includes('task-counts'),
      );
      expect(posts.length).toBe(2);
      const keys = posts.map((c) => JSON.parse((c[1] as RequestInit).body as string).idempotency_key);
      expect(keys[0]).toBeTruthy();
      expect(keys[0]).toBe(keys[1]);
    });
  });

  it('treats a status:"duplicate" add response as a clean success (no error)', async () => {
    const user = userEvent.setup();
    const addToast = vi.fn();
    fetchSpy.mockImplementation((url: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = url.toString();
      const isAddPost =
        init?.method === 'POST' && urlStr.includes('/directions') && !urlStr.includes('task-counts');
      if (isAddPost) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ direction: { id: 'd-1' }, status: 'duplicate' }),
        } as Response);
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ directions: [] }) } as Response);
    });

    render(<DirectionsPanel addToast={addToast} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await user.click(await screen.findByRole('button', { name: 'New direction' }));
    const input = await screen.findByPlaceholderText('New direction...');
    const addBtn = input.parentElement?.querySelector('button');
    if (!addBtn) throw new Error('test setup: expected add button');

    await user.click(addBtn);

    await waitFor(() => {
      const posts = fetchSpy.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST' && c[0].toString().includes('/directions') && !c[0].toString().includes('task-counts'),
      );
      expect(posts.length).toBe(1);
    });
    // Duplicate replay routes to success — the add form is reset, no error toast.
    expect(formMock.resetAddForm).toHaveBeenCalled();
    expect(addToast).not.toHaveBeenCalledWith(expect.anything(), 'error');
  });

  it('renders the strategist-working indicator when strategistActive is true', async () => {
    mockFetch(baseResponses);
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} strategistActive />);
    await screen.findByRole('button', { name: 'New direction' });
    expect(screen.getByText('Strategist working…')).toBeInTheDocument();
  });

  it('hides the strategist-working indicator when strategistActive is absent/false', async () => {
    mockFetch(baseResponses);
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} />);
    await screen.findByRole('button', { name: 'New direction' });
    expect(screen.queryByText('Strategist working…')).toBeNull();
  });

  it('shows generic "Strategist working…" when strategistActive=true and no cycleProgress (minimal event backward-compat)', async () => {
    mockFetch(baseResponses);
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} strategistActive cycleProgress={null} />);
    await screen.findByRole('button', { name: 'New direction' });
    expect(screen.getByText('Strategist working…')).toBeInTheDocument();
  });

  it('shows phase label from enriched cycleProgress event', async () => {
    mockFetch(baseResponses);
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} strategistActive cycleProgress={{ task_id: 't1', phase_label: 'Reviewing directions' }} />);
    await screen.findByRole('button', { name: 'New direction' });
    expect(screen.getByText('Strategist: Reviewing directions…')).toBeInTheDocument();
  });

  it('shows steering excerpt under the phase label in directions panel', async () => {
    mockFetch(baseResponses);
    render(<DirectionsPanel addToast={vi.fn()} selectedProjectId="proj1" projects={TEST_PROJECTS} strategistActive cycleProgress={{ task_id: 't1', phase_label: 'Evaluating', steering_excerpt: 'Focus on reliability' }} />);
    await screen.findByRole('button', { name: 'New direction' });
    expect(screen.getByText('Strategist: Evaluating…')).toBeInTheDocument();
    expect(screen.getByText(/"Focus on reliability"/)).toBeInTheDocument();
  });

  it('shows cycle-completion receipt when lastCycleCompletion is set and strategist is idle', async () => {
    mockFetch(baseResponses);
    render(
      <DirectionsPanel

        addToast={vi.fn()}
        selectedProjectId="proj1"
        projects={TEST_PROJECTS}
        strategistActive={false}
        lastCycleCompletion={{ status: 'completed', created_count: 2, tasks_spawned: 3 }}
      />
    );
    await screen.findByRole('button', { name: 'New direction' });
    expect(screen.getByText('Cycle complete')).toBeInTheDocument();
    expect(screen.getByText(/\+2 direction/)).toBeInTheDocument();
    expect(screen.getByText(/3 tasks spawned/)).toBeInTheDocument();
  });

  it('hides cycle-completion receipt while strategist is still active', async () => {
    mockFetch(baseResponses);
    render(
      <DirectionsPanel

        addToast={vi.fn()}
        selectedProjectId="proj1"
        projects={TEST_PROJECTS}
        strategistActive
        lastCycleCompletion={{ status: 'completed', created_count: 1 }}
      />
    );
    await screen.findByRole('button', { name: 'New direction' });
    expect(screen.queryByText('Cycle complete')).toBeNull();
  });
});
