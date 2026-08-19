import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicStreamingPreview } from './StrategicStreamingPreview';
import type {
  StrategicLiveActivity,
  StrategicStreamingTurn,
} from '../utils/strategicSessionLive';
import { expectNoOverflowAt } from '../test/viewport';

describe('StrategicStreamingPreview — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  async function renderPreview(props: {
    turn: StrategicStreamingTurn | null;
    activity: StrategicLiveActivity;
  }) {
    host = document.createElement('div');
    host.style.width = '100%';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<StrategicStreamingPreview turn={props.turn} activity={props.activity} />);
    });
  }

  const emptyActivity: StrategicLiveActivity = { items: [], latestLifecycle: null };

  test('a long streamed reply with reasoning does not overflow at 375px', async () => {
    const turn: StrategicStreamingTurn = {
      key: 'sess-1:1',
      sessionId: 'sess-1',
      text: 'A long streamed answer that must wrap inside the narrow chat column instead of pushing the layout wider than the viewport.',
      segments: [
        { kind: 'reasoning', text: 'Checking backend/app/release_commit_authorship.py for the candidate range guard before answering.' },
        { kind: 'reply', text: 'A long streamed answer that must wrap inside the narrow chat column instead of pushing the layout wider than the viewport.' },
      ],
    };
    await expectNoOverflowAt(375, () => renderPreview({ turn, activity: emptyActivity }));
  });

  test('a full activity strip with long command lines does not overflow at 375px', async () => {
    const activity: StrategicLiveActivity = {
      latestLifecycle: 'responding',
      items: Array.from({ length: 8 }, (_, index) => ({
        id: index + 1,
        kind: index % 2 === 0 ? 'command' : 'read',
        text: `bash scripts/test-backend.sh backend/testing/test_release_commit_authorship.py --verbose-run-${index}`,
        inFlight: index === 7,
      })),
    };
    await expectNoOverflowAt(375, () => renderPreview({ turn: null, activity }));
  });
});
