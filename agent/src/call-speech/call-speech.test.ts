import { describe, expect, it, vi } from 'vitest';

import {
  CallSpeechConfigurationError,
  loadCallSpeechConfig,
  parseCallSpeechCredentialEnvelope,
  redactProviderError,
  withoutCallSpeechSecrets,
} from './config.js';
import { DeepgramFluxStt, type SpeechWebSocket } from './deepgram-flux.js';
import { applyConfirmedTheoryCorrection, theoryCorrectionCandidate } from './theory-text.js';
import { TtsPhraseQueue } from './tts-queue.js';
import { CartesiaSonicTts, type PhraseSynthesizer, type PhraseSynthesisRequest } from './cartesia-sonic.js';
import type { SpeechLatencyMetrics } from './types.js';

const CONFIG = {
  deepgram: {
    apiKey: 'deepgram-secret',
    model: 'flux-general-en',
    endpoint: 'wss://api.deepgram.com/v2/listen',
    sampleRateHz: 16000,
    eotThreshold: 0.7,
    eagerEotThreshold: 0.5,
    eotTimeoutMs: 900,
    connectTimeoutMs: 500,
  },
  cartesia: {
    apiKey: 'cartesia-secret',
    model: 'sonic-3.5',
    voiceId: 'voice-id',
    endpoint: 'wss://api.cartesia.ai/tts/websocket',
    apiVersion: '2026-03-01',
    sampleRateHz: 16000,
    connectTimeoutMs: 500,
  },
} as const;

class FakeSocket implements SpeechWebSocket {
  readonly sent: Array<string | Buffer> = [];
  private readonly listeners = new Map<string, Array<(...args: never[]) => void>>();

  on(event: 'open' | 'close' | 'error' | 'message', listener: (...args: never[]) => void): this {
    const listeners = this.listeners.get(event) ?? [];
    listeners.push(listener);
    this.listeners.set(event, listeners);
    return this;
  }

  send(data: string | Buffer): void {
    this.sent.push(data);
  }

  close(): void {
    this.emit('close');
  }

  emit(event: 'open' | 'close' | 'error' | 'message', ...args: unknown[]): void {
    for (const listener of this.listeners.get(event) ?? []) {
      (listener as (...values: unknown[]) => void)(...args);
    }
  }
}

function metrics(): SpeechLatencyMetrics {
  return { inputAudioMs: 0, outputAudioBytes: 0, outputCharacters: 0, providerErrors: 0 };
}

class ControlledSynthesizer implements PhraseSynthesizer {
  readonly requests: PhraseSynthesisRequest[] = [];
  private readonly pending = new Map<string, () => void>();
  fail = false;

  async start(): Promise<void> {}

  async synthesize(request: PhraseSynthesisRequest): Promise<void> {
    this.requests.push(request);
    if (this.fail) throw new Error('provider unavailable');
    await new Promise<void>((resolve) => this.pending.set(request.phraseId, resolve));
  }

  cancel(phraseId: string): void {
    this.pending.get(phraseId)?.();
    this.pending.delete(phraseId);
  }

  complete(phraseId: string): void {
    this.pending.get(phraseId)?.();
    this.pending.delete(phraseId);
  }

  async close(): Promise<void> {}
  metrics(): SpeechLatencyMetrics {
    return metrics();
  }
}

async function nextTick(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('call speech configuration and theoretical corrections', () => {
  it('fails closed without each required provider setting', () => {
    expect(() => loadCallSpeechConfig({})).toThrow(CallSpeechConfigurationError);
    expect(() => loadCallSpeechConfig({
      CALL_DEEPGRAM_API_KEY: 'a',
      CALL_DEEPGRAM_STT_MODEL: 'flux-general-en',
      CALL_CARTESIA_API_KEY: 'b',
      CALL_CARTESIA_TTS_MODEL: 'sonic-3.5',
    })).toThrow('CALL_CARTESIA_VOICE_ID');
    expect(() => loadCallSpeechConfig({
      CALL_DEEPGRAM_API_KEY: 'a',
      CALL_DEEPGRAM_STT_MODEL: 'flux-general-en',
      CALL_CARTESIA_API_KEY: 'b',
      CALL_CARTESIA_TTS_MODEL: 'sonic-3.5',
      CALL_CARTESIA_VOICE_ID: 'voice',
      CALL_DEEPGRAM_STT_ENDPOINT: 'ws://not-safe.example/listen',
    })).toThrow('wss://');
  });

  it('accepts only the finite call-scoped backend credential envelope', () => {
    const speech = parseCallSpeechCredentialEnvelope({
      speech: {
        CALL_DEEPGRAM_API_KEY: 'a',
        CALL_DEEPGRAM_STT_MODEL: 'flux-general-en',
        CALL_CARTESIA_API_KEY: 'b',
        CALL_CARTESIA_TTS_MODEL: 'sonic-3.5',
        CALL_CARTESIA_VOICE_ID: 'voice',
      },
    });
    expect(speech.CALL_DEEPGRAM_API_KEY).toBe('a');
    expect(() => parseCallSpeechCredentialEnvelope({
      speech: { ...speech, BLUEPRINT_AGENT_TOKEN: 'not-allowed' },
    })).toThrow('unsupported field');
  });

  it('does not silently rewrite ETA or pi, while making notation a confirmable candidate', () => {
    expect(theoryCorrectionCandidate('ETA for the train is five minutes')).toBeUndefined();
    expect(theoryCorrectionCandidate('I want pie after this call')).toBeUndefined();
    const candidate = theoryCorrectionCandidate('the beta distribution has x sub i');
    expect(candidate).toEqual({ text: 'the β distribution has x_i', reason: 'theory_notation', requiresConfirmation: true });
    if (!candidate) throw new Error('expected theory correction candidate');
    expect(applyConfirmedTheoryCorrection('the beta distribution has x sub i', candidate.text)).toBe(candidate.text);
    expect(() => applyConfirmedTheoryCorrection('ETA for the train is five minutes', 'η for the train is five minutes')).toThrow('Rejected');
  });

  it('redacts provider credentials from operational errors', () => {
    const redacted = redactProviderError('deepgram', new Error('Authorization: Token deepgram-secret?api_key=also-secret'));
    expect(redacted).not.toContain('deepgram-secret');
    expect(redacted).not.toContain('also-secret');
    expect(redacted).toContain('[redacted]');
  });

  it('removes speech credentials from the Fast Resident child environment', () => {
    expect(withoutCallSpeechSecrets({ PATH: 'safe', CALL_DEEPGRAM_API_KEY: 'not-for-fast', CALL_CARTESIA_API_KEY: 'not-for-fast' }))
      .toEqual({ PATH: 'safe' });
  });
});

describe('Deepgram Flux STT', () => {
  it('uses Flux endpointing, preserves canonical transcript, and emits a final turn', async () => {
    const socket = new FakeSocket();
    let requestedUrl = '';
    const events: Array<{ kind: string; transcript: string; candidate?: string }> = [];
    const stt = new DeepgramFluxStt(CONFIG.deepgram, {
      onTranscript: (event) => events.push({ kind: event.kind, transcript: event.transcript, candidate: event.correctionCandidate?.text }),
    }, (url) => {
      requestedUrl = url;
      return socket;
    });
    const start = stt.start();
    socket.emit('open');
    await start;
    expect(new URL(requestedUrl).searchParams.get('mip_opt_out')).toBe('true');
    expect(String(socket.sent[0])).toContain('"type":"Configure"');
    socket.emit('message', Buffer.from(JSON.stringify({ type: 'TurnInfo', event: 'StartOfTurn', turn_index: 2 })));
    socket.emit('message', Buffer.from(JSON.stringify({ type: 'TurnInfo', event: 'EagerEndOfTurn', transcript: 'the beta distribution', turn_index: 2 })));
    socket.emit('message', Buffer.from(JSON.stringify({ type: 'TurnInfo', event: 'EndOfTurn', transcript: 'the beta distribution', turn_index: 2 })));
    expect(events).toEqual([
      { kind: 'turn_start', transcript: '', candidate: undefined },
      { kind: 'eager_turn_end', transcript: 'the beta distribution', candidate: 'the β distribution' },
      { kind: 'final', transcript: 'the beta distribution', candidate: 'the β distribution' },
      { kind: 'turn_end', transcript: 'the beta distribution', candidate: 'the β distribution' },
    ]);
    stt.pushPcm({ pcm: Buffer.alloc(16000 / 25 * 2), sampleRateHz: 16000, channels: 1 });
    expect(socket.sent.at(-1)).toBeInstanceOf(Buffer);
    expect(() => {
      stt.pushPcm({ pcm: Buffer.alloc(16000 / 5 * 2), sampleRateHz: 16000, channels: 1 });
    }).toThrow('80ms');
  });

  it('contains provider errors instead of leaking their payloads', async () => {
    const socket = new FakeSocket();
    const states: string[] = [];
    const stt = new DeepgramFluxStt(CONFIG.deepgram, { onProviderState: (state) => { states.push(state.reason ?? state.state); } }, () => socket);
    const start = stt.start();
    socket.emit('open');
    await start;
    socket.emit('message', Buffer.from(JSON.stringify({ type: 'Error', description: 'Authorization: Token deepgram-secret' })));
    expect(states.join(' ')).not.toContain('deepgram-secret');
    expect(states.at(-1)).toContain('[redacted]');
  });

  it('rejects a pre-open close rather than leaving Flux connecting', async () => {
    const socket = new FakeSocket();
    const stt = new DeepgramFluxStt(CONFIG.deepgram, {}, () => socket);
    const starting = stt.start();
    socket.emit('close');
    await expect(starting).rejects.toThrow('closed before Deepgram Flux');
  });
});

describe('TTS phrase queue', () => {
  it('times out Cartesia startup instead of exposing a permanent connecting call', async () => {
    vi.useFakeTimers();
    try {
      const socket = new FakeSocket();
      const tts = new CartesiaSonicTts(CONFIG.cartesia, {}, () => socket);
      const starting = tts.start();
      const expected = expect(starting).rejects.toThrow('timed out before Cartesia Sonic');
      await vi.advanceTimersByTimeAsync(CONFIG.cartesia.connectTimeoutMs);
      await expected;
    } finally {
      vi.useRealTimers();
    }
  });

  it('streams Cartesia PCM and aligned word captions over the documented websocket context', async () => {
    const socket = new FakeSocket();
    const audio: Buffer[] = [];
    const captions: string[] = [];
    const tts = new CartesiaSonicTts(CONFIG.cartesia, {}, () => socket);
    const start = tts.start();
    socket.emit('open');
    await start;
    const phrase = tts.synthesize({
      phraseId: 'phrase-1',
      text: 'A mathematical sentence.',
      signal: new AbortController().signal,
      onAudio: (event) => audio.push(event.pcm),
      onCaption: (event) => captions.push(`${event.words.join(' ')}:${event.startMs.join(',')}`),
    });
    expect(String(socket.sent[0])).toContain('"model_id":"sonic-3.5"');
    socket.emit('message', Buffer.from(JSON.stringify({ type: 'chunk', context_id: 'phrase-1', data: Buffer.from([1, 2, 3, 4]).toString('base64') })));
    socket.emit('message', Buffer.from(JSON.stringify({
      type: 'timestamps', context_id: 'phrase-1', word_timestamps: { words: ['A', 'sentence'], start: [0, 0.25], end: [0.1, 0.5] },
    })));
    socket.emit('message', Buffer.from(JSON.stringify({ type: 'done', context_id: 'phrase-1', done: true })));
    await phrase;
    expect(audio).toEqual([Buffer.from([1, 2, 3, 4])]);
    expect(captions).toEqual(['A sentence:0,250']);
  });

  it('cancels at the queue boundary without starting a stale next phrase', async () => {
    const provider = new ControlledSynthesizer();
    const played: string[] = [];
    const lifecycle: string[] = [];
    const queue = new TtsPhraseQueue(provider, {
      onAudio: (event) => played.push(event.phraseId),
      onPhraseLifecycle: (event) => lifecycle.push(`${event.phraseId}:${event.event}`),
    });
    const speaking = queue.speak('First sentence. Second sentence.');
    await nextTick();
    expect(provider.requests).toHaveLength(1);
    const startedAt = Date.now();
    await queue.cancel('barge-in');
    expect(Date.now() - startedAt).toBeLessThan(250);
    await speaking;
    expect(provider.requests).toHaveLength(1);
    const firstRequest = provider.requests.at(0);
    if (!firstRequest) throw new Error('expected first speech request');
    firstRequest.onAudio({ kind: 'audio', phraseId: firstRequest.phraseId, pcm: Buffer.alloc(8), sampleRateHz: 16000, channels: 1 });
    expect(played).toEqual([]);
    expect(lifecycle).toEqual([`${firstRequest.phraseId}:interrupted`]);
  });

  it('emits phrase lifecycle only after real audio and successful synthesis', async () => {
    const provider = new ControlledSynthesizer();
    const lifecycle: string[] = [];
    const queue = new TtsPhraseQueue(provider, {
      onPhraseLifecycle: (event) => lifecycle.push(`${event.phraseId}:${event.event}`),
    });
    const speaking = queue.speak('One sentence.');
    await nextTick();
    const request = provider.requests.at(0);
    if (!request) throw new Error('expected speech request');
    expect(lifecycle).toEqual([]);
    request.onAudio({
      kind: 'audio',
      phraseId: request.phraseId,
      pcm: Buffer.alloc(8),
      sampleRateHz: 16_000,
      channels: 1,
    });
    expect(lifecycle).toEqual([`${request.phraseId}:started`]);
    provider.complete(request.phraseId);
    await speaking;
    expect(lifecycle).toEqual([
      `${request.phraseId}:started`,
      `${request.phraseId}:completed`,
    ]);
  });

  it('surfaces a provider failure rather than swallowing it', async () => {
    const provider = new ControlledSynthesizer();
    provider.fail = true;
    const queue = new TtsPhraseQueue(provider);
    await expect(queue.speak('A sentence.')).rejects.toThrow('provider unavailable');
  });
});
