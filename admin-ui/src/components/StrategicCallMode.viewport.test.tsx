import { afterEach, describe, test } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { StrategicCallMode } from './StrategicCallMode';
import { expectFullViewportAt } from '../test/viewport';

describe('StrategicCallMode — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  function mountActiveCall() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    root = createRoot(host);
    return act(async () => {
      root?.render(
        <StrategicCallMode
          enabled
          connectionState="connected"
          microphoneState="on"
          audioPlaybackState="blocked"
          captions={[
            {
              id: 'resident-long-caption',
              speaker: 'resident',
              final: true,
              text: 'The resident is explaining a deliberately long mathematical result so the live-caption line must wrap cleanly on a narrow operator screen.',
            },
            {
              id: 'operator-long-caption',
              speaker: 'operator',
              final: false,
              text: 'I am asking a deliberately long follow-up question while keeping the call controls reachable on a narrow screen.',
            },
          ]}
          displayBlocks={[
            {
              id: 'long-artifact-preview',
              type: 'artifact_ref',
              title: 'Long live result preview',
              content: 'The current artifact records the posterior estimate, confidence interval, and the exact Whiteboard revision that the Resident used for this response.',
            },
          ]}
          error="Browser audio playback is blocked until you explicitly enable it."
          onStart={() => {}}
          onEnd={() => {}}
          onReconnect={() => {}}
          onToggleMicrophone={() => {}}
          onEnableAudio={() => {}}
        />,
      );
    });
  }

  test('keeps active-call controls, captions, and live results usable at 375px', async () => {
    await expectFullViewportAt(375, mountActiveCall);
  });

  test('keeps active-call controls, captions, and live results usable at 768px', async () => {
    await expectFullViewportAt(768, mountActiveCall);
  });
});
