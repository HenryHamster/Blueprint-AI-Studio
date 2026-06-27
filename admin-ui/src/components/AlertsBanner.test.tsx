import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AlertsBanner } from './AlertsBanner';
import type { Alert } from '../types/alert';

vi.mock('../api', () => ({
  apiFetch: vi.fn(),
}));

const alert: Alert = {
  id: 'alert-1',
  project_id: 'project-1',
  severity: 'WARNING',
  title: 'Warm pool saturation',
  message: 'capacity crossed the warning threshold',
  domain: 'warm_pool',
  event_type: 'capacity.warning',
  source: 'warm_pool',
  dedupe_key: 'project-1:warm_pool:capacity',
  occurrence_count: 3,
  last_seen_at: '2026-06-27T08:00:00',
  task_id: null,
  created_at: '2026-06-27T07:00:00',
  dismissed_at: null,
};

describe('AlertsBanner', () => {
  it('renders additive alert metadata fields', () => {
    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} />);

    expect(screen.getByText('Domain warm pool')).toBeInTheDocument();
    expect(screen.getByText('Event capacity warning')).toBeInTheDocument();
    expect(screen.getByText('Source warm pool')).toBeInTheDocument();
    expect(screen.getByText('3 occurrences')).toBeInTheDocument();
    expect(screen.queryByText(/Dedupe/)).not.toBeInTheDocument();
  });
});
