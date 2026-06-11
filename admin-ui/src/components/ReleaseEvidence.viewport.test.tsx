import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import type { LiveReleaseEvidence } from '../api';
import { ReleaseEvidenceChip, ReleaseEvidencePanel } from './ReleaseEvidence';

const evidence: LiveReleaseEvidence = {
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
      reason_code: 'agent image not reachable from promoted live proof',
    },
  ],
};

describe('ReleaseEvidence - viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) {
      const mountedRoot = root;
      act(() => mountedRoot.unmount());
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  async function renderEvidence() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    root = createRoot(host);
    const mountedRoot = root;
    await act(async () => {
      mountedRoot.render(
        <div className="space-y-3">
          <ReleaseEvidencePanel evidence={evidence} />
          <div className="max-w-full">
            <ReleaseEvidenceChip evidence={evidence} />
          </div>
        </div>,
      );
    });
  }

  test('panel and compact chip render without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, renderEvidence);
  });

  test('panel and compact chip labels do not silently clip at 375px', async () => {
    await expectNoTextTruncationAt(375, renderEvidence);
  });
});
