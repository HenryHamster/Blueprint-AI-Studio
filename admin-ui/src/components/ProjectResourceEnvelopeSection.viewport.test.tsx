import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectResourceEnvelopeSection } from './ProjectResourceEnvelopeSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('ProjectResourceEnvelopeSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <ProjectResourceEnvelopeSection
            githubOwner="myorg"
            githubRepo="myrepo"
            data={{
              project_id: 'project-1',
              max_concurrent_vms: 4,
              spend_ceiling_usd: 50,
              spend_window_days: 14,
              active_vm_count: 2,
              recent_spend_usd: 7.25,
              recent_spend_compute_usd: 4.0,
              recent_spend_agent_vm_usd: 2.25,
              recent_spend_idle_usd: 1.0,
              is_throttled: false,
            }}
            loading={false}
            error={null}
            canSave={true}
            onSave={async () => {}}
          />,
        );
      });
    });
  });
});
