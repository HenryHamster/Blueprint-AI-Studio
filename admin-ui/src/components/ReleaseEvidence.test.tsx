import { describe, test, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { ReleaseEvidencePanel, ReleaseEvidenceChip } from './ReleaseEvidence';
import { shortPromotedSha, asLiveReleaseEvidence } from './ReleaseEvidence.utils';
import type { LiveReleaseEvidence } from '../api';

const fullEvidence: LiveReleaseEvidence = {
  promoted_sha: 'abcdef1234567890deadbeef',
  promoted_at: '2026-06-05T00:00:00Z',
  aggregate_serving_status: 'serving_confirmed',
  aggregate_reason_code: null,
  release_version_id: 'rv-8',
  release_version_number: 8,
  required_artifacts: [
    {
      artifact_kind: 'backend_revision',
      required: true,
      serving_status: 'serving_confirmed',
      served_identifier: 'abcdef1234567890',
      served_at: '2026-06-05T00:00:00Z',
      reason_code: null,
    },
    {
      artifact_kind: 'agent_image',
      required: true,
      serving_status: 'deploy_failed',
      served_identifier: null,
      served_at: null,
      reason_code: 'agent image not reachable',
    },
  ],
};

describe('ReleaseEvidence helpers', () => {
  test('shortPromotedSha truncates to 8 chars and tolerates missing values', () => {
    expect(shortPromotedSha('abcdef1234567890')).toBe('abcdef12');
    expect(shortPromotedSha(null)).toBeNull();
    expect(shortPromotedSha(undefined)).toBeNull();
    expect(shortPromotedSha('')).toBeNull();
  });

  test('asLiveReleaseEvidence coerces objects and rejects non-objects', () => {
    expect(asLiveReleaseEvidence(fullEvidence)).toBe(fullEvidence);
    expect(asLiveReleaseEvidence(null)).toBeNull();
    expect(asLiveReleaseEvidence(undefined)).toBeNull();
    expect(asLiveReleaseEvidence('nope')).toBeNull();
    expect(asLiveReleaseEvidence(42)).toBeNull();
  });
});

describe('ReleaseEvidencePanel', () => {
  afterEach(() => cleanup());

  test('renders version, short SHA, and serving status when evidence is present', () => {
    const { getByTestId } = render(<ReleaseEvidencePanel evidence={fullEvidence} />);
    const panel = getByTestId('release-evidence-panel');
    expect(panel.textContent).toContain('Live Proof Context');
    expect(panel.textContent).toContain('v8');
    expect(panel.textContent).toContain('abcdef12');
    expect(panel.textContent).not.toContain('abcdef1234567890deadbeef');
    expect(panel.textContent).toContain('serving_confirmed');
    expect(panel.textContent).toContain('Required artifacts');
    expect(panel.textContent).toContain('backend_revision');
    expect(panel.textContent).toContain('agent_image');
    expect(panel.textContent).toContain('deploy_failed');
    expect(panel.textContent).toContain('agent image not reachable');
  });

  test('renders nothing when evidence is null or undefined', () => {
    const { container, rerender } = render(<ReleaseEvidencePanel evidence={null} />);
    expect(container.firstChild).toBeNull();
    rerender(<ReleaseEvidencePanel evidence={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  test('tolerates missing optional fields', () => {
    const { getByTestId } = render(
      <ReleaseEvidencePanel evidence={{ aggregate_serving_status: 'deploy_failed' }} />,
    );
    const panel = getByTestId('release-evidence-panel');
    // No version / SHA, but the status still renders (and is not serving_confirmed → amber path).
    expect(panel.textContent).toContain('Live Proof Context');
    expect(panel.textContent).toContain('deploy_failed');
    // No version chip and no SHA prefix when those fields are absent.
    expect(panel.textContent).not.toContain('v8');
    expect(panel.textContent).not.toContain('sha ');
  });
});

describe('ReleaseEvidenceChip', () => {
  afterEach(() => cleanup());

  test('renders a compact live-proof indicator', () => {
    const { getByTestId } = render(<ReleaseEvidenceChip evidence={fullEvidence} />);
    const chip = getByTestId('release-evidence-chip');
    expect(chip.textContent).toContain('Live proof of v8');
    expect(chip.textContent).toContain('abcdef12');
    expect(chip.textContent).toContain('backend_revision');
    expect(chip.textContent).toContain('serving_confirmed');
    expect(chip.textContent).toContain('agent_image');
    expect(chip.textContent).toContain('deploy_failed');
    expect(chip.textContent).toContain('agent image not reachable');
  });

  test('renders nothing when evidence is absent', () => {
    const { container } = render(<ReleaseEvidenceChip evidence={null} />);
    expect(container.firstChild).toBeNull();
  });

  test('omits the version suffix when release_version_number is missing', () => {
    const { getByTestId } = render(
      <ReleaseEvidenceChip evidence={{ promoted_sha: 'cafebabe0000', aggregate_serving_status: 'unconfirmed' }} />,
    );
    const chip = getByTestId('release-evidence-chip');
    expect(chip.textContent).toContain('Live proof');
    expect(chip.textContent).not.toContain('of v');
    expect(chip.textContent).toContain('cafebabe');
  });
});
