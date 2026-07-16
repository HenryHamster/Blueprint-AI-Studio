import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// firebase.ts calls getAuth() at module load; stub it out so tests don't need env vars
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

vi.mock('../api', () => ({
  fetchRetirementCritiques: vi.fn(),
  fetchResearchActionability: vi.fn(),
}));

import { StrategicAuditPanel } from './StrategicAuditPanel';
import * as api from '../api';

const mockFetchCritiques = vi.mocked(api.fetchRetirementCritiques);
const mockFetchActionability = vi.mocked(api.fetchResearchActionability);

function makeActionability(overrides: Partial<api.ActionabilityResponse> = {}): api.ActionabilityResponse {
  return {
    cycles_scanned: 5,
    total_findings: 10,
    grade_distribution: { HIGH: 4, MEDIUM: 4, LOW: 2 },
    grade_tasked: { HIGH: 3, MEDIUM: 2, LOW: 1 },
    grade_merged: { HIGH: 2, MEDIUM: 1, LOW: 0 },
    ...overrides,
  };
}

function makeCritique(overrides: Partial<api.RetirementCritique> = {}): api.RetirementCritique {
  return {
    direction_id: 'dir-123',
    what_it_addressed: 'Reduce query latency',
    pattern_that_produced_it: 'Over-broad scope',
    lesson: 'Keep directions focused on one system',
    cycle_id: 'cycle-abc',
    cycle_created_at: '2026-04-01T00:00:00',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('StrategicAuditPanel', () => {
  it('shows loading skeleton while fetching', () => {
    mockFetchCritiques.mockReturnValue(new Promise(() => {}));
    mockFetchActionability.mockReturnValue(new Promise(() => {}));

    const { container } = render(<StrategicAuditPanel projectId="proj-1" />);
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });

  it('renders grade distribution with all three tiers', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(makeActionability());

    render(<StrategicAuditPanel projectId="proj-1" />);

    await waitFor(() => expect(screen.getByText('Research Actionability')).toBeInTheDocument());

    expect(screen.getByText('HIGH')).toBeInTheDocument();
    expect(screen.getByText('MEDIUM')).toBeInTheDocument();
    expect(screen.getByText('LOW')).toBeInTheDocument();
    expect(screen.getByText('(5 cycles · 10 findings)')).toBeInTheDocument();
  });

  it('renders all four critique fields', async () => {
    const critique = makeCritique();
    mockFetchCritiques.mockResolvedValue({ critiques: [critique], total: 1 });
    mockFetchActionability.mockResolvedValue(makeActionability({ total_findings: 0 }));

    render(<StrategicAuditPanel projectId="proj-1" />);

    await waitFor(() => expect(screen.getByText('Retirement Critiques')).toBeInTheDocument());

    expect(screen.getByText('Reduce query latency')).toBeInTheDocument();
    expect(screen.getByText('Over-broad scope')).toBeInTheDocument();
    expect(screen.getByText('Keep directions focused on one system')).toBeInTheDocument();
    expect(screen.getByText('dir-123')).toBeInTheDocument();
  });

  it('renders nothing when both data streams are empty', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(makeActionability({ total_findings: 0 }));

    const { container } = render(<StrategicAuditPanel projectId="proj-1" />);

    await waitFor(() => expect(container.querySelector('.animate-pulse')).not.toBeInTheDocument());

    expect(container.firstChild).toBeNull();
  });

  it('shows error message on fetch failure', async () => {
    mockFetchCritiques.mockRejectedValue(new Error('Network error'));
    mockFetchActionability.mockRejectedValue(new Error('Network error'));

    render(<StrategicAuditPanel projectId="proj-1" />);

    await waitFor(() => expect(screen.getByText('Network error')).toBeInTheDocument());
  });

  it('renders multiple critiques from a single cycle', async () => {
    const critiques = [
      makeCritique({ direction_id: 'dir-a', lesson: 'Lesson A' }),
      makeCritique({ direction_id: 'dir-b', lesson: 'Lesson B' }),
    ];
    mockFetchCritiques.mockResolvedValue({ critiques, total: 2 });
    mockFetchActionability.mockResolvedValue(makeActionability({ total_findings: 0 }));

    render(<StrategicAuditPanel projectId="proj-1" />);

    await waitFor(() => expect(screen.getByText('(2)')).toBeInTheDocument());

    expect(screen.getByText('Lesson A')).toBeInTheDocument();
    expect(screen.getByText('Lesson B')).toBeInTheDocument();
  });

  it('renders green calibration badge when score meets the floor', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(
      makeActionability({ calibration_score: 0.35 }),
    );

    render(<StrategicAuditPanel projectId="proj-1" />);

    const badge = await screen.findByTestId('calibration-badge');
    expect(badge).toHaveTextContent('+35%');
    expect(badge.className).toContain('emerald');
    expect(badge).not.toHaveAttribute('title');
  });

  it('renders amber calibration badge when score is below floor but non-negative', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(
      makeActionability({ calibration_score: 0.02 }),
    );

    render(<StrategicAuditPanel projectId="proj-1" />);

    const badge = await screen.findByTestId('calibration-badge');
    expect(badge).toHaveTextContent('+2%');
    expect(badge.className).toContain('amber');
    expect(badge).toHaveAttribute('title');
    expect(badge.getAttribute('title')).toMatch(/HIGH-grade findings/i);
  });

  it('renders red calibration badge when score is negative', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(
      makeActionability({ calibration_score: -0.05 }),
    );

    render(<StrategicAuditPanel projectId="proj-1" />);

    const badge = await screen.findByTestId('calibration-badge');
    expect(badge).toHaveTextContent('-5%');
    expect(badge.className).toContain('red');
    expect(badge).toHaveAttribute('title');
  });

  it('renders n/a calibration badge with neutral style when score is null', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(
      makeActionability({ calibration_score: null }),
    );

    render(<StrategicAuditPanel projectId="proj-1" />);

    const badge = await screen.findByTestId('calibration-badge');
    expect(badge).toHaveTextContent('n/a');
    expect(badge.className).toContain('neutral');
    expect(badge.className).not.toContain('amber');
    expect(badge.className).not.toContain('red');
  });

  it('respects a custom calibrationFloor for the green/amber boundary', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(
      makeActionability({ calibration_score: 0.10 }),
    );

    // Default floor 0.05 → 0.10 would be green; with floor 0.20 it falls to amber.
    render(<StrategicAuditPanel projectId="proj-1" calibrationFloor={0.20} />);

    const badge = await screen.findByTestId('calibration-badge');
    expect(badge).toHaveTextContent('+10%');
    expect(badge.className).toContain('amber');
  });

  it('renders per-grade merge rate from grade_merge_rate', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(
      makeActionability({
        grade_merge_rate: { HIGH: 0.5, MEDIUM: 0.25, LOW: null },
      }),
    );

    render(<StrategicAuditPanel projectId="proj-1" />);

    await waitFor(() => expect(screen.getByText('Research Actionability')).toBeInTheDocument());

    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByText('25%')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('renders cycle_history sparkline with one square per cycle', async () => {
    mockFetchCritiques.mockResolvedValue({ critiques: [], total: 0 });
    mockFetchActionability.mockResolvedValue(
      makeActionability({
        cycle_history: [
          {
            cycle_id: 'c1',
            cycle_created_at: '2026-04-01T00:00:00',
            total_findings: 3,
            grade_distribution: { HIGH: 1, MEDIUM: 1, LOW: 1 },
            grade_merged: { HIGH: 1, MEDIUM: 0, LOW: 0 },
            grade_merge_rate: { HIGH: 1, MEDIUM: 0, LOW: 0 },
            calibration_score: 1.0,
          },
          {
            cycle_id: 'c2',
            cycle_created_at: null,
            total_findings: 0,
            grade_distribution: {},
            grade_merged: {},
            grade_merge_rate: { HIGH: null, MEDIUM: null, LOW: null },
            calibration_score: null,
          },
        ],
      }),
    );

    render(<StrategicAuditPanel projectId="proj-1" />);

    const sparkline = await screen.findByTestId('calibration-sparkline');
    expect(sparkline.children).toHaveLength(2);
  });
});
