import { randomUUID } from 'node:crypto';

import WebSocket, { type RawData } from 'ws';

import { redactProviderError, type CallSpeechConfig } from './config.js';
import type {
  SpeechAudioEvent,
  SpeechEventSink,
  SpeechLatencyMetrics,
  SpeechProviderState,
  TheoryCaptionEvent,
} from './types.js';
import type { SpeechWebSocket, SpeechWebSocketFactory } from './deepgram-flux.js';

const MAX_PHRASE_CHARACTERS = 240;
const MAX_AUDIO_CHUNK_BYTES = 64 * 1024;

interface CartesiaEvent {
  type?: string;
  context_id?: string;
  data?: string;
  done?: boolean;
  title?: string;
  message?: string;
  word_timestamps?: { words?: string[]; start?: number[]; end?: number[] };
}

export interface PhraseSynthesisRequest {
  phraseId: string;
  text: string;
  signal: AbortSignal;
  onAudio(event: SpeechAudioEvent): void;
  onCaption(event: TheoryCaptionEvent): void;
}

export interface PhraseSynthesizer {
  start(): Promise<void>;
  synthesize(request: PhraseSynthesisRequest): Promise<void>;
  cancel(phraseId: string): void;
  close(): Promise<void>;
  metrics(): SpeechLatencyMetrics;
}

interface PendingPhrase {
  request: PhraseSynthesisRequest;
  resolve: () => void;
  reject: (error: Error) => void;
  cancelListener: () => void;
}

const createWebSocket: SpeechWebSocketFactory = (url, options) => new WebSocket(url, options);

function asText(data: RawData): string {
  if (typeof data === 'string') return data;
  if (Array.isArray(data)) return Buffer.concat(data).toString('utf8');
  if (Buffer.isBuffer(data)) return data.toString('utf8');
  return Buffer.from(data).toString('utf8');
}

function emptyMetrics(): SpeechLatencyMetrics {
  return { inputAudioMs: 0, outputAudioBytes: 0, outputCharacters: 0, providerErrors: 0 };
}

/**
 * Direct Cartesia WebSocket transport.  A coordinator can keep this socket
 * warm while TtsPhraseQueue creates a distinct context for every phrase.
 */
export class CartesiaSonicTts implements PhraseSynthesizer {
  private socket: SpeechWebSocket | undefined;
  private readonly pending = new Map<string, PendingPhrase>();
  private readonly metricsValue = emptyMetrics();
  private state: SpeechProviderState = { provider: 'cartesia', state: 'closed', fallback: 'none' };

  constructor(
    private readonly config: CallSpeechConfig['cartesia'],
    private readonly sink: SpeechEventSink = {},
    private readonly websocketFactory: SpeechWebSocketFactory = createWebSocket,
  ) {}

  async start(): Promise<void> {
    if (this.socket) return;
    this.setState({ provider: 'cartesia', state: 'connecting', fallback: 'none' });
    const url = new URL(this.config.endpoint);
    url.searchParams.set('cartesia_version', this.config.apiVersion);
    const socket = this.websocketFactory(url.toString(), { headers: { 'X-API-Key': this.config.apiKey } });
    this.socket = socket;
    await new Promise<void>((resolve, reject) => {
      let settled = false;
      const settle = (callback: () => void): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        callback();
      };
      const timeout = setTimeout(() => {
        const error = new Error('connection timed out before Cartesia Sonic became ready');
        this.fail(error);
        this.socket = undefined;
        settle(() => {
          reject(new Error(redactProviderError('cartesia', error)));
        });
        socket.close();
      }, this.config.connectTimeoutMs);
      socket.on('open', () => {
        this.setState({ provider: 'cartesia', state: 'ready', fallback: 'none' });
        settle(resolve);
      });
      socket.on('message', (data: unknown) => {
        this.onMessage(data as RawData);
      });
      socket.on('error', (error: unknown) => {
        this.fail(error);
        this.socket = undefined;
        settle(() => {
          reject(new Error(redactProviderError('cartesia', error)));
        });
      });
      socket.on('close', () => {
        this.socket = undefined;
        if (!settled) {
          const error = new Error('connection closed before Cartesia Sonic became ready');
          this.fail(error);
          settle(() => {
            reject(new Error(redactProviderError('cartesia', error)));
          });
          return;
        }
        for (const [phraseId] of this.pending) this.rejectPhrase(phraseId, new Error('Cartesia speech socket closed.'));
        if (this.state.state !== 'failed') this.setState({ provider: 'cartesia', state: 'closed', fallback: 'none' });
      });
    });
  }

  async synthesize(request: PhraseSynthesisRequest): Promise<void> {
    if (!this.socket || this.state.state !== 'ready') throw new Error('Cartesia speech synthesizer is not ready.');
    if (!request.phraseId || request.text.length === 0 || request.text.length > MAX_PHRASE_CHARACTERS) {
      throw new Error(`TTS phrase must be between 1 and ${MAX_PHRASE_CHARACTERS} characters.`);
    }
    if (request.signal.aborted) return;
    const socket = this.socket;
    await new Promise<void>((resolve, reject) => {
      const cancelListener = () => {
        this.cancel(request.phraseId);
      };
      const pending: PendingPhrase = { request, resolve, reject, cancelListener };
      this.pending.set(request.phraseId, pending);
      request.signal.addEventListener('abort', cancelListener, { once: true });
      socket.send(
        JSON.stringify({
          model_id: this.config.model,
          transcript: request.text,
          voice: { mode: 'id', id: this.config.voiceId },
          language: 'en',
          context_id: request.phraseId,
          output_format: { container: 'raw', encoding: 'pcm_s16le', sample_rate: this.config.sampleRateHz },
          add_timestamps: true,
          continue: false,
        }),
      );
      this.metricsValue.outputCharacters += request.text.length;
    });
  }

  cancel(phraseId: string): void {
    const pending = this.pending.get(phraseId);
    if (!pending) return;
    this.socket?.send(JSON.stringify({ context_id: phraseId, cancel: true }));
    this.resolvePhrase(phraseId);
  }

  close(): Promise<void> {
    for (const [phraseId] of this.pending) this.cancel(phraseId);
    this.socket?.close();
    this.socket = undefined;
    this.setState({ provider: 'cartesia', state: 'closed', fallback: 'none' });
    return Promise.resolve();
  }

  metrics(): SpeechLatencyMetrics {
    return { ...this.metricsValue };
  }

  private onMessage(raw: RawData): void {
    let event: CartesiaEvent;
    try {
      event = JSON.parse(asText(raw)) as CartesiaEvent;
    } catch {
      this.fail('invalid Cartesia websocket message');
      return;
    }
    const phraseId = event.context_id;
    if (!phraseId) return;
    const pending = this.pending.get(phraseId);
    if (!pending) return; // Late data after a barge-in must never be replayed.
    if (event.type === 'chunk' && event.data) {
      const pcm = Buffer.from(event.data, 'base64');
      if (pcm.length === 0 || pcm.length > MAX_AUDIO_CHUNK_BYTES) {
        this.rejectPhrase(phraseId, new Error('Cartesia emitted an invalid audio chunk.'));
        return;
      }
      this.metricsValue.outputAudioBytes += pcm.length;
      pending.request.onAudio({ kind: 'audio', phraseId, pcm, sampleRateHz: this.config.sampleRateHz, channels: 1 });
      return;
    }
    if (event.type === 'timestamps' && event.word_timestamps) {
      const words = event.word_timestamps.words ?? [];
      const startMs = (event.word_timestamps.start ?? []).map((seconds) => Math.round(seconds * 1000));
      const endMs = (event.word_timestamps.end ?? []).map((seconds) => Math.round(seconds * 1000));
      if (words.length === startMs.length && words.length === endMs.length) {
        pending.request.onCaption({ kind: 'caption', phraseId, words, startMs, endMs });
      }
      return;
    }
    if (event.type === 'error') {
      this.rejectPhrase(phraseId, new Error(redactProviderError('cartesia', event.message ?? event.title ?? 'provider error')));
      this.fail(event.message ?? event.title ?? 'provider error');
      return;
    }
    if (event.type === 'done' || event.done) this.resolvePhrase(phraseId);
  }

  private resolvePhrase(phraseId: string): void {
    const pending = this.pending.get(phraseId);
    if (!pending) return;
    pending.request.signal.removeEventListener('abort', pending.cancelListener);
    this.pending.delete(phraseId);
    pending.resolve();
  }

  private rejectPhrase(phraseId: string, error: Error): void {
    const pending = this.pending.get(phraseId);
    if (!pending) return;
    pending.request.signal.removeEventListener('abort', pending.cancelListener);
    this.pending.delete(phraseId);
    pending.reject(error);
  }

  private fail(error: unknown): void {
    this.metricsValue.providerErrors += 1;
    this.setState({ provider: 'cartesia', state: 'failed', fallback: 'none', reason: redactProviderError('cartesia', error) });
  }

  private setState(state: SpeechProviderState): void {
    this.state = state;
    this.sink.onProviderState?.(state);
  }
}

export function newPhraseId(): string {
  return randomUUID();
}
