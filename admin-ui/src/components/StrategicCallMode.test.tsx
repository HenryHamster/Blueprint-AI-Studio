import { afterEach, describe, expect, test } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { StrategicCallLauncher, StrategicCallMode } from './StrategicCallMode';

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

  test('does not occupy conversation space while voice is off', () => {
    render();
    expect(host?.querySelector('[data-testid="strategic-call-mode"]')).toBeNull();
  });

  test('provides a compact composer launcher with the privacy disclosure', () => {
    host = document.createElement('div');
    document.body.append(host);
    root = createRoot(host);
    act(() => root?.render(<StrategicCallLauncher onStart={() => {}} />));
    expect(host.querySelector('button')?.textContent).toContain('Call');
    expect(host.textContent).toContain('does not record raw call audio');
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
    expect(host?.textContent).toContain('Listening now');
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
    expect(host!.textContent).toContain('there is no transcription or reply audio yet');
    expect(host!.textContent).toContain('End call');
    expect(host!.textContent).not.toContain('call active');
  });

  test('keeps the active surface from being flex-shrunk out of view', () => {
    render({ enabled: true, connectionState: 'connected', microphoneState: 'on' });
    expect(host?.querySelector('[data-testid="strategic-call-mode"]')?.classList).toContain('shrink-0');
  });

  test('discloses configured call services before microphone activation', () => {
    host = document.createElement('div');
    document.body.append(host);
    root = createRoot(host);
    act(() => root?.render(<StrategicCallLauncher onStart={() => {}} />));
    expect(host!.textContent).toContain('transcribed by Deepgram');
    expect(host!.textContent).toContain('voiced by Cartesia');
    expect(host!.textContent).toContain('does not record raw call audio');
  });

  test('offers a visible mute control during an active call', () => {
    render({ enabled: true, connectionState: 'connected', microphoneState: 'on' });
    const button = host!.querySelector<HTMLButtonElement>('[aria-label="Mute microphone"]');
    expect(button?.textContent).toContain('Mute');
    expect(button?.getAttribute('aria-pressed')).toBe('true');
    expect(host!.textContent).toContain('Listening now');
  });

  test('makes mute transitions explicit and prevents a second click from reversing them', () => {
    render({ enabled: true, connectionState: 'connected', microphoneState: 'disabling' });
    const button = host!.querySelector<HTMLButtonElement>('[aria-label="Mute microphone"]');
    expect(button?.textContent).toContain('Muting');
    expect(button?.disabled).toBe(true);
    expect(host!.textContent).toContain('Muting the microphone');
  });

  test('clearly confirms that a completed mute persists', () => {
    render({ enabled: true, connectionState: 'connected', microphoneState: 'off' });
    const button = host!.querySelector<HTMLButtonElement>('[aria-label="Unmute microphone"]');
    expect(button?.textContent).toContain('Unmute');
    expect(button?.getAttribute('aria-pressed')).toBe('false');
    expect(host!.textContent).toContain('Muted — the Resident cannot hear you');
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
