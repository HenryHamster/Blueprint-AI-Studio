import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const { apiFetch } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch,
  API_BASE_URL: '',
}));

vi.mock('./messageArming', () => ({
  parseArmingOutcome: vi.fn(() => null),
  armingToastCopy: vi.fn(() => 'Message sent'),
  composerHintCopy: vi.fn(() => ''),
}));

vi.mock('../utils/idempotencyKey', () => ({
  generateIdempotencyKey: vi.fn(() => 'test-key'),
}));

vi.mock('../utils/time', () => ({
  timeAgo: vi.fn(() => '5m ago'),
}));

import { ComposeDrawer } from './ComposeDrawer';

const defaultProps = {
  projectId: 'proj-1',
  userEmail: 'user@example.com',
  onOpenFullPanel: vi.fn(),
  onClose: vi.fn(),
  addToast: vi.fn(),
};

describe('ComposeDrawer', () => {
  beforeEach(() => {
    apiFetch.mockResolvedValue(
      new Response(JSON.stringify({
        messages: [
          { id: 'm1', content: 'Hello recent', message_type: 'FEEDBACK', created_at: '2026-01-01T00:00:00Z', from_type: 'user' },
          { id: 'm2', content: 'Another message', message_type: 'NOTE', created_at: '2026-01-02T00:00:00Z', from_type: 'user' },
        ],
      }), { status: 200 })
    );
  });

  it('clicking a Recent message populates the composer with that message type and content', async () => {
    render(<ComposeDrawer {...defaultProps} />);

    await waitFor(() => screen.getByText('Hello recent'));

    const recentBtn = screen.getByText('Hello recent').closest('button');
    expect(recentBtn).not.toBeNull();

    fireEvent.click(recentBtn!);

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    expect(textarea.value).toBe('Hello recent');
  });
});
