import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { ConsultationProgressRow } from './ConsultationProgressRow';
import type { ConsultationDetail, ConsultationIndexRow } from '../consultationDetailApi';

// Only the network read is replaced; the state-labelling logic under test is
// the real module's, so a wrong label here is a real wrong label.
const { fetchDetail } = vi.hoisted(() => ({ fetchDetail: vi.fn() }));

vi.mock('../consultationDetailApi', async importOriginal => {
  const actual = await importOriginal<typeof import('../consultationDetailApi')>();
  return { ...actual, fetchConsultationDetail: fetchDetail };
});

function indexRow(overrides: Partial<ConsultationIndexRow> = {}): ConsultationIndexRow {
  return {
    consultation_id: 'c-1',
    state: 'running',
    outcome: null,
    requested_at: new Date(Date.now() - 40_000).toISOString(),
    question_preview: { text: 'why did promotion stall', truncated: false, chars: 23 },
    step_count: 2,
    steps_truncated: false,
    claim_expired: false,
    delivered_at: null,
    ...overrides,
  };
}

function detail(overrides: Partial<ConsultationDetail> = {}): ConsultationDetail {
  return {
    consultation_id: 'c-1',
    state: 'delivered',
    outcome: 'completed',
    requested_at: new Date(Date.now() - 400_000).toISOString(),
    continuation_key: 'k-1',
    question: { text: 'why did promotion stall', truncated: false, chars: 23 },
    packet: { id: 'p-1', created_at: null, updated_at: null, handling_state: 'RESPONDED' },
    claim: {
      claimed_by_task_id: 'aaaabbbbccccdddd',
      claim_id: 'cl-1',
      incarnation: 'inc-1',
      generation: 3,
      attempt: 2,
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      expired: false,
    },
    steps: [{ op: 'read_task', entity_kind: 'task', entity_id: '1234567890', effect: 'inspected' }],
    steps_truncated: false,
    last_failure: null,
    result: {
      id: 'r-1',
      created_at: null,
      summary: { text: 'CI was red on the scripts lane', truncated: false, chars: 30 },
      receipts: [{ op: 'requeue_task', entity_kind: 'task', entity_id: '1234567890' }],
      receipts_have_more: false,
    },
    delivered_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('ConsultationProgressRow', () => {
  beforeEach(() => {
    fetchDetail.mockReset();
    fetchDetail.mockResolvedValue(detail());
  });
  afterEach(() => { vi.clearAllMocks(); });

  test('names an expired lease instead of letting it read as still working', () => {
    render(
      <ConsultationProgressRow
        projectId="p"
        consultationId="c-1"
        row={indexRow({ state: 'stalled', claim_expired: true })}
      />,
    );

    // `handling_state` is CLAIMED for both running and stalled — the whole
    // point of the server-side `expired` flag is that this line differs.
    expect(screen.getByText(/lease expired/i)).toBeInTheDocument();
    expect(screen.queryByText(/Workday investigating/)).toBeNull();
    // Collapsed means collapsed: no detail request until someone opens it.
    expect(fetchDetail).not.toHaveBeenCalled();
  });

  test('shows a live sub-session as one line with its step count and question', () => {
    render(
      <ConsultationProgressRow projectId="p" consultationId="c-1" row={indexRow()} />,
    );

    expect(screen.getByText('Workday investigating')).toBeInTheDocument();
    expect(screen.getByText(/2 steps/)).toBeInTheDocument();
    expect(screen.getByText('why did promotion stall')).toBeInTheDocument();
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'false');
  });

  test('expanding loads the record once and shows what the sub-session did', async () => {
    const user = userEvent.setup();
    render(
      <ConsultationProgressRow
        projectId="p"
        consultationId="c-1"
        row={indexRow({ state: 'delivered', outcome: 'completed', step_count: 1,
          delivered_at: new Date().toISOString() })}
      />,
    );

    await user.click(screen.getByRole('button'));

    await waitFor(() => expect(screen.getByText('Asked')).toBeInTheDocument());
    expect(screen.getByText('Returned')).toBeInTheDocument();
    expect(screen.getByText('CI was red on the scripts lane')).toBeInTheDocument();
    expect(screen.getByText(/read_task · task 12345678 · inspected/)).toBeInTheDocument();
    expect(screen.getByText(/requeue_task · task 12345678/)).toBeInTheDocument();
    expect(screen.getByText(/attempt 2/)).toBeInTheDocument();
    expect(fetchDetail).toHaveBeenCalledTimes(1);
    expect(fetchDetail).toHaveBeenCalledWith('p', 'c-1', expect.anything());
  });

  test('a rejected consultation carries its own diagnosis', async () => {
    fetchDetail.mockResolvedValue(detail({
      state: 'delivered',
      outcome: 'failed',
      result: null,
      last_failure: {
        gate: 'write_scope_denied',
        detail: 'operator approval required',
        recorded_at: new Date().toISOString(),
      },
    }));
    const user = userEvent.setup();
    render(
      <ConsultationProgressRow
        projectId="p"
        consultationId="c-1"
        row={indexRow({ state: 'delivered', outcome: 'failed' })}
      />,
    );

    expect(screen.getByText('Workday could not finish')).toBeInTheDocument();
    await user.click(screen.getByRole('button'));
    await waitFor(() =>
      expect(screen.getByText(/write_scope_denied: operator approval required/))
        .toBeInTheDocument());
  });

  test('renders nothing until the index knows about the consultation', () => {
    const { container } = render(
      <ConsultationProgressRow projectId="p" consultationId="c-1" row={null} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(fetchDetail).not.toHaveBeenCalled();
  });
});
