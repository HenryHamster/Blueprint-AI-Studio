import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ReleaseQualificationFindings } from './ReleaseQualificationFindings';
import * as api from '../api';
import type { RequalificationReadiness } from '../api';

vi.mock('../api');

function makeReadiness(
  overrides: Partial<RequalificationReadiness> = {},
): RequalificationReadiness {
  return {
    requalification_ready: false,
    open_version_id: 'ver-uuid-open',
    audit_findings_truncated: false,
    gating_total: 3,
    gating_resolved: 1,
    reason: '2 of 3 carried high/medium audit finding(s) still unresolved',
    findings: [
      { key: 'adb632b709685086', severity: 'high', summary: 'Finding already fixed', state: 'resolved' },
      { key: 'deadbeef12345678', severity: 'medium', summary: 'Finding no longer applies', state: 'stale' },
      { key: 'abcdef0123456789', severity: 'high', summary: 'Blocking finding needs disposal', state: 'unresolved' },
    ],
    ...overrides,
  };
}

describe('ReleaseQualificationFindings', () => {
  const mockRefresh = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mockRefresh.mockClear();
    vi.mocked(api.recordFindingResolution).mockResolvedValue({
      id: 'row-1',
      project_id: 'proj-1',
      release_version_id: 'ver-uuid-open',
      finding_key: 'abcdef0123456789',
      state: 'resolved',
      severity: 'high',
      note: 'fixed in PR #1234',
      resolving_ref: null,
      verified_against_sha: null,
    });
  });

  it('renders nothing when readiness is absent', () => {
    const { container } = render(
      <ReleaseQualificationFindings readiness={null} projectId="proj-1" />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders the readiness headline, reason, and per-finding state badges', () => {
    render(<ReleaseQualificationFindings readiness={makeReadiness()} projectId="proj-1" />);
    expect(screen.getByTestId('requalification-ready-badge')).toHaveTextContent(
      'blocked by carried findings',
    );
    expect(screen.getByTestId('requalification-reason')).toHaveTextContent('2 of 3');
    const rows = screen.getAllByTestId('requalification-finding');
    expect(rows).toHaveLength(3);
    // findings are sorted by severity (high before medium), stable within a severity
    expect(rows[0].getAttribute('data-finding-state')).toBe('resolved');
    expect(rows[1].getAttribute('data-finding-state')).toBe('unresolved');
    expect(rows[2].getAttribute('data-finding-state')).toBe('stale');
  });

  it('shows dispose controls only for unresolved findings', () => {
    render(<ReleaseQualificationFindings readiness={makeReadiness()} projectId="proj-1" />);
    expect(screen.getAllByTestId('resolve-finding')).toHaveLength(1);
    expect(screen.getAllByTestId('stale-finding')).toHaveLength(1);
  });

  it('hides dispose controls in readOnly mode', () => {
    render(
      <ReleaseQualificationFindings readiness={makeReadiness()} projectId="proj-1" readOnly />,
    );
    expect(screen.queryAllByTestId('resolve-finding')).toHaveLength(0);
    expect(screen.queryAllByTestId('stale-finding')).toHaveLength(0);
  });

  it('marks an unresolved finding resolved with a note and refreshes the parent', async () => {
    render(
      <ReleaseQualificationFindings
        readiness={makeReadiness()}
        projectId="proj-1"
        onRefresh={mockRefresh}
      />,
    );
    fireEvent.click(screen.getByTestId('resolve-finding'));
    expect(screen.getByText('Mark finding resolved?')).toBeTruthy();
    fireEvent.change(screen.getByTestId('confirm-dialog-attestation-input'), {
      target: { value: 'fixed in PR #1234' },
    });
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(api.recordFindingResolution).toHaveBeenCalledWith('proj-1', {
        finding_key: 'abcdef0123456789',
        release_version_id: 'ver-uuid-open',
        state: 'resolved',
        severity: 'high',
        note: 'fixed in PR #1234',
      });
    });
    expect(mockRefresh).toHaveBeenCalled();
  });

  it('marks a finding stale via the stale button', async () => {
    render(
      <ReleaseQualificationFindings
        readiness={makeReadiness()}
        projectId="proj-1"
        onRefresh={mockRefresh}
      />,
    );
    fireEvent.click(screen.getByTestId('stale-finding'));
    expect(screen.getByText('Mark finding stale?')).toBeTruthy();
    fireEvent.change(screen.getByTestId('confirm-dialog-attestation-input'), {
      target: { value: 'no longer applies to current code' },
    });
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(api.recordFindingResolution).toHaveBeenCalledWith('proj-1', {
        finding_key: 'abcdef0123456789',
        release_version_id: 'ver-uuid-open',
        state: 'stale',
        severity: 'high',
        note: 'no longer applies to current code',
      });
    });
  });

  it('surfaces an endpoint failure as an action message without refreshing', async () => {
    vi.mocked(api.recordFindingResolution).mockRejectedValue(
      new Error('Release version not found for this project.'),
    );
    render(
      <ReleaseQualificationFindings
        readiness={makeReadiness()}
        projectId="proj-1"
        onRefresh={mockRefresh}
      />,
    );
    fireEvent.click(screen.getByTestId('resolve-finding'));
    fireEvent.change(screen.getByTestId('confirm-dialog-attestation-input'), {
      target: { value: 'reason' },
    });
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('requalification-action-msg')).toHaveTextContent(
        'Release version not found',
      );
    });
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it('shows a ready badge and no dispose controls when all gating findings are resolved', () => {
    render(
      <ReleaseQualificationFindings
        readiness={makeReadiness({
          requalification_ready: true,
          gating_resolved: 3,
          reason:
            'all 3 carried high/medium audit finding(s) resolved or code-verified stale — ready to re-qualify',
          findings: [
            { key: 'a', severity: 'high', summary: 'x', state: 'resolved' },
            { key: 'b', severity: 'medium', summary: 'y', state: 'stale' },
            { key: 'c', severity: 'high', summary: 'z', state: 'resolved' },
          ],
        })}
        projectId="proj-1"
      />,
    );
    expect(screen.getByTestId('requalification-ready-badge')).toHaveTextContent(
      'ready to re-qualify',
    );
    expect(screen.queryAllByTestId('resolve-finding')).toHaveLength(0);
  });
});
