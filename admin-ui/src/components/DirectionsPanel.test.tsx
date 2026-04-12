import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DirectionsPanel } from './DirectionsPanel';

vi.mock('../api', () => ({
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***' }),
  API_BASE_URL: '',
}));

vi.mock('./DirectionCard', () => ({
  DirectionCard: ({ direction }: { direction: { text: string } }) => (
    <div data-testid="direction-card">{direction.text}</div>
  ),
}));

vi.mock('./DesignDocViewer', () => ({
  default: () => <div data-testid="design-doc-viewer">DesignDoc</div>,
}));

vi.mock('./VisionViewer', () => ({
  default: () => <div data-testid="vision-viewer">VisionContent</div>,
}));

vi.mock('./AutonomousConfigSection', () => ({
  AutonomousConfigSection: () => <div>AutonomousConfig</div>,
}));

vi.mock('../hooks/useDirectionForm', () => ({
  useDirectionForm: () => ({
    newText: 'Test direction',
    setNewText: vi.fn(),
    newPriority: 'medium',
    setNewPriority: vi.fn(),
    newPathTags: [],
    setNewPathTags: vi.fn(),
    newPathInput: '',
    setNewPathInput: vi.fn(),
    editingId: null,
    setEditingId: vi.fn(),
    editText: '',
    editPriority: 'medium',
    editStatus: 'active',
    editPathTags: [],
    editPathInput: '',
    setEditText: vi.fn(),
    setEditPriority: vi.fn(),
    setEditStatus: vi.fn(),
    setEditPathTags: vi.fn(),
    setEditPathInput: vi.fn(),
    startEdit: vi.fn(),
    resetAddForm: vi.fn(),
  }),
}));

vi.mock('../hooks/useAutonomousConfig', () => ({
  useAutonomousConfig: () => ({
    fetchCooldownSetting: vi.fn(),
    fetchSettings: vi.fn(),
    configCooldownHours: 24,
    setConfigCooldownHours: vi.fn(),
    configIntervalMinutes: 60,
    setConfigIntervalMinutes: vi.fn(),
    configWaveSize: 3,
    setConfigWaveSize: vi.fn(),
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

const baseResponses = {
  '/api/admin/projects/proj1/directions/task-counts': { counts: {} },
  '/api/admin/projects/proj1/directions': {
    directions: [],
    autonomous_enabled: false,
    vision: {},
    design_doc: {},
  },
  '/api/admin/projects': [{ id: 'proj1', name: 'Project 1' }],
};

describe('DirectionsPanel', () => {
  const originalError = console.error;

  beforeEach(() => {
    console.error = vi.fn();
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    console.error = originalError;
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('fetches projects on mount and renders them in the selector', async () => {
    mockFetch(baseResponses);
    render(<DirectionsPanel onClose={vi.fn()} addToast={vi.fn()} />);
    expect(await screen.findByText('Project 1')).toBeInTheDocument();
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
    render(<DirectionsPanel onClose={vi.fn()} addToast={vi.fn()} />);
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
    render(<DirectionsPanel onClose={vi.fn()} addToast={vi.fn()} />);
    await screen.findByText('Active direction');
    // Section headers render as "{label} ({count})"
    expect(screen.getAllByText('Active (1)').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Proposed by Agent (1)').length).toBeGreaterThan(0);
  });

  it('clicking the Vision tab shows VisionViewer', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    render(<DirectionsPanel onClose={vi.fn()} addToast={vi.fn()} />);
    await screen.findByText('Project 1');
    await user.click(screen.getByRole('button', { name: 'Vision' }));
    expect(screen.getByTestId('vision-viewer')).toBeInTheDocument();
  });

  it('clicking the Design Doc tab shows DesignDocViewer', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    render(<DirectionsPanel onClose={vi.fn()} addToast={vi.fn()} />);
    await screen.findByText('Project 1');
    await user.click(screen.getByRole('button', { name: 'Design Doc' }));
    expect(screen.getByTestId('design-doc-viewer')).toBeInTheDocument();
  });

  it('clicking the Add button triggers a POST fetch for the direction', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    render(<DirectionsPanel onClose={vi.fn()} addToast={vi.fn()} />);
    // Wait for form to appear (requires selectedProjectId to be set after project fetch)
    const input = await screen.findByPlaceholderText('New direction...');
    // Add button is the only button in the same flex row as the input
    const addBtn = input.parentElement!.querySelector('button')!;
    await user.click(addBtn);
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        expect.stringContaining('/directions'),
        expect.objectContaining({ method: 'POST' })
      );
    });
  });

  it('clicking the close backdrop fires onClose', async () => {
    mockFetch(baseResponses);
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { container } = render(<DirectionsPanel onClose={onClose} addToast={vi.fn()} />);
    await screen.findByText('Project 1');
    // Backdrop is the div with both "absolute" and "inset-0" classes
    const backdrop = container.querySelector('.absolute.inset-0');
    await user.click(backdrop as HTMLElement);
    expect(onClose).toHaveBeenCalledOnce();
  });
});
