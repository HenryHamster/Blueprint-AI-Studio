import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import ProblemModelViewer from './ProblemModelViewer';
import RunningUpdatePanel from './RunningUpdatePanel';
import type { ProblemModel } from '../types';
import {
  apiFetch,
  grantCapabilityProofExemption,
  revokeCapabilityProofExemption,
} from '../api';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: vi.fn(),
    grantCapabilityProofExemption: vi.fn(),
    revokeCapabilityProofExemption: vi.fn(),
  });
});

describe('project read-only editors', () => {
  beforeEach(() => vi.clearAllMocks());

  test('running update viewer gets document content with no mutation controls or requests', () => {
    const onAcknowledged = vi.fn();
    const onEdited = vi.fn();
    render(
      <RunningUpdatePanel
        readOnly
        projectId="project-1"
        document="# Current\n\nReadable snapshot"
        acknowledgedAt={null}
        addToast={vi.fn()}
        onAcknowledged={onAcknowledged}
        onEdited={onEdited}
      />,
    );

    expect(screen.getByText('current snapshot')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge' })).not.toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
    expect(onAcknowledged).not.toHaveBeenCalled();
    expect(onEdited).not.toHaveBeenCalled();
  });

  test('problem model viewer hides edit, direction, link, and proof-exemption mutations', () => {
    const onUpdated = vi.fn();
    const onSpawnDirection = vi.fn();
    const onLinkDirection = vi.fn();
    const problemModel: ProblemModel = {
      intent: { purpose: 'Readable purpose', current_focus: 'Read-only focus' },
      capabilities: [{
        name: 'Deploy safely',
        status: 'working',
        proof_status: 'unproven',
      }],
      gaps: [{
        id: 'gap-1',
        name: 'Close the gap',
        description: 'Read-only gap detail',
        priority: 'high',
        size: 'medium',
        status: 'active',
        active_moves: [],
        past_moves: [],
        history: [],
      }],
    };

    render(
      <ProblemModelViewer
        readOnly
        projectId="project-1"
        problemModel={problemModel}
        addToast={vi.fn()}
        onUpdated={onUpdated}
        onSpawnDirection={onSpawnDirection}
        onLinkDirection={onLinkDirection}
        directions={[{ id: 'direction-1', text: 'Existing direction' }]}
      />,
    );

    expect(screen.getByText('Readable purpose')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit intent' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Spawn direction/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByTestId('capability-exempt-toggle-Deploy safely')).not.toBeInTheDocument();
    expect(grantCapabilityProofExemption).not.toHaveBeenCalled();
    expect(revokeCapabilityProofExemption).not.toHaveBeenCalled();
    expect(onUpdated).not.toHaveBeenCalled();
    expect(onSpawnDirection).not.toHaveBeenCalled();
    expect(onLinkDirection).not.toHaveBeenCalled();
  });
});
