import { afterEach, describe, expect, test } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { StrategicCallMode } from './StrategicCallMode';

describe('StrategicCallMode', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    host?.remove();
    host = null;
  });

  function render(props: Partial<React.ComponentProps<typeof StrategicCallMode>> = {}) {
    host = document.createElement('div');
    document.body.append(host);
    root = createRoot(host);
    act(() => root?.render(<StrategicCallMode
      enabled={false}
      connectionState="idle"
      microphoneState="off"
      audioPlaybackState="unknown"
      captions={[]}
      displayBlocks={[]}
      error={null}
      onStart={() => {}}
      onEnd={() => {}}
      onReconnect={() => {}}
      onToggleMicrophone={() => {}}
      onEnableAudio={() => {}}
      {...props}
    />));
  }

  test('keeps typed conversation available while voice is off', () => {
    render();
    expect(host?.textContent).toContain('typed messages still use this conversation');
    expect(host?.querySelector('button')?.textContent).toContain('Start call');
  });

  test('renders only ephemeral captions and byte-preserving theory blocks during an active call', () => {
    render({
      enabled: true,
      connectionState: 'connected',
      microphoneState: 'on',
      captions: [{ id: 'caption-1', text: 'The answer uses eta sub t.', final: true, speaker: 'resident' }],
      displayBlocks: [{ id: 'formula-1', type: 'latex', content: '\\eta_t = \\mathbb{E}[X_t]' }],
    });
    expect(host?.querySelector('[data-testid="strategic-call-captions"]')?.textContent).toContain('eta sub t');
    expect(host?.querySelector('[data-testid="strategic-call-display-blocks"]')?.textContent).toContain('\\eta_t');
    expect(host?.textContent).toContain('One participant only');
  });

  test('keeps the end-call escape hatch visible when an active lease has a media error', () => {
    render({
      enabled: true,
      connectionState: 'error',
      error: 'Speech transport unavailable.',
    });

    expect(host!.textContent).toContain('End call');
    expect(host!.textContent).toContain('Call needs attention');
    expect(host!.textContent).toContain('Speech transport unavailable.');
  });

  test('does not present an RTC-only connection as a usable Resident call', () => {
    render({ enabled: true, connectionState: 'waiting_for_resident', microphoneState: 'on' });
    expect(host!.textContent).toContain('waiting for Resident');
    expect(host!.textContent).toContain('End call');
    expect(host!.textContent).not.toContain('call active');
  });

  test('discloses configured call services before microphone activation', () => {
    render();
    expect(host!.textContent).toContain('transcribed by Deepgram');
    expect(host!.textContent).toContain('voiced by Cartesia');
    expect(host!.textContent).toContain('does not record the raw call audio');
  });

  test('offers a visible mute control during an active call', () => {
    render({ enabled: true, connectionState: 'connected', microphoneState: 'on' });
    expect(host!.querySelector('[aria-label="Mute microphone"]')?.textContent).toContain('Mute');
  });

  test('offers a direct user-gesture audio recovery control when autoplay is blocked', () => {
    render({
      enabled: true,
      connectionState: 'connected',
      microphoneState: 'on',
      audioPlaybackState: 'blocked',
    });
    expect(host!.textContent).toContain('Enable audio');
  });
});
