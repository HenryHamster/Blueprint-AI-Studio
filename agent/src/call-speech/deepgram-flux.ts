import WebSocket, { type RawData } from 'ws';

import { redactProviderError, type CallSpeechConfig } from './config.js';
import { buildTheoryKeyterms, theoryCorrectionCandidate } from './theory-text.js';
import type {
  PcmAudioFrame,
  SpeechEventSink,
  SpeechLatencyMetrics,
  SpeechProviderState,
  StreamingStt,
  TranscriptEvent,
} from './types.js';

interface FluxMessage {
  type?: string;
  event?: string;
  transcript?: string;
  turn_index?: number;
  end_of_turn_confidence?: number;
  audio_window_end?: number;
  description?: string;
  message?: string;
}

export interface SpeechWebSocket {
  on(event: 'open' | 'close' | 'error' | 'message', listener: (...args: never[]) => void): this;
  send(data: string | Buffer): void;
  close(): void;
}

export type SpeechWebSocketFactory = (url: string, options: { headers: Record<string, string> }) => SpeechWebSocket;

const createWebSocket: SpeechWebSocketFactory = (url, options) => new WebSocket(url, options);
const MAX_PCM_MILLISECONDS = 80;

function asText(data: RawData): string {
  if (typeof data === 'string') return data;
  if (Array.isArray(data)) return Buffer.concat(data).toString('utf8');
  if (Buffer.isBuffer(data)) return data.toString('utf8');
  return Buffer.from(data).toString('utf8');
}

function emptyMetrics(): SpeechLatencyMetrics {
  return { inputAudioMs: 0, outputAudioBytes: 0, outputCharacters: 0, providerErrors: 0 };
}

/** Direct Flux-v2 websocket adapter. The coordinator owns this instance. */
export class DeepgramFluxStt implements StreamingStt {
  private socket: SpeechWebSocket | undefined;
  private readonly metricsValue = emptyMetrics();
  private readonly startedAt = Date.now();
  private keyterms: string[];
  private state: SpeechProviderState = { provider: 'deepgram', state: 'closed', fallback: 'none' };

  constructor(
    private readonly config: CallSpeechConfig['deepgram'],
    private readonly sink: SpeechEventSink = {},
    private readonly websocketFactory: SpeechWebSocketFactory = createWebSocket,
  ) {
    this.keyterms = buildTheoryKeyterms();
  }

  async start(): Promise<void> {
    if (this.socket) return;
    this.setState({ provider: 'deepgram', state: 'connecting', fallback: 'none' });
    const url = new URL(this.config.endpoint);
    url.searchParams.set('model', this.config.model);
    url.searchParams.set('encoding', 'linear16');
    url.searchParams.set('sample_rate', String(this.config.sampleRateHz));
    // Deepgram's Model Improvement Partnership is opt-out per request. Call
    // mode must not silently trade research audio/transcripts for discounted
    // inference, so make the privacy-preserving behavior unconditional.
    url.searchParams.set('mip_opt_out', 'true');
    const socket = this.websocketFactory(url.toString(), {
      headers: { Authorization: `Token ${this.config.apiKey}` },
    });
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
        const error = new Error('connection timed out before Deepgram Flux became ready');
        this.fail(error);
        this.socket = undefined;
        settle(() => {
          reject(new Error(redactProviderError('deepgram', error)));
        });
        socket.close();
      }, this.config.connectTimeoutMs);
      socket.on('open', () => {
        this.setState({ provider: 'deepgram', state: 'ready', fallback: 'none' });
        this.sendConfigure();
        settle(resolve);
      });
      socket.on('message', (data: unknown) => {
        this.onMessage(data as RawData);
      });
      socket.on('error', (error: unknown) => {
        this.fail(error);
        this.socket = undefined;
        settle(() => {
          reject(new Error(redactProviderError('deepgram', error)));
        });
      });
      socket.on('close', () => {
        this.socket = undefined;
        if (!settled) {
          const error = new Error('connection closed before Deepgram Flux became ready');
          this.fail(error);
          settle(() => {
            reject(new Error(redactProviderError('deepgram', error)));
          });
          return;
        }
        if (this.state.state !== 'failed') this.setState({ provider: 'deepgram', state: 'closed', fallback: 'none' });
      });
    });
  }

  pushPcm(frame: PcmAudioFrame): void {
    if (frame.channels !== 1 || frame.sampleRateHz !== this.config.sampleRateHz || frame.pcm.length === 0) {
      throw new Error(`Deepgram Flux requires non-empty mono ${this.config.sampleRateHz}Hz PCM.`);
    }
    if (frame.pcm.length % 2 !== 0) throw new Error('PCM must contain complete signed 16-bit samples.');
    const durationMs = (frame.pcm.length / 2 / frame.sampleRateHz) * 1000;
    if (durationMs > MAX_PCM_MILLISECONDS) {
      throw new Error(`PCM frame exceeds ${MAX_PCM_MILLISECONDS}ms bound.`);
    }
    if (!this.socket || this.state.state !== 'ready') throw new Error('Deepgram speech recognizer is not ready.');
    this.metricsValue.inputAudioMs += durationMs;
    this.socket.send(frame.pcm);
  }

  updateTheoryKeyterms(keyterms: readonly string[]): void {
    this.keyterms = buildTheoryKeyterms(keyterms);
    if (this.state.state === 'ready') this.sendConfigure();
  }

  close(): Promise<void> {
    if (!this.socket) return Promise.resolve();
    try {
      this.socket.send(JSON.stringify({ type: 'CloseStream' }));
    } finally {
      this.socket.close();
      this.socket = undefined;
      this.setState({ provider: 'deepgram', state: 'closed', fallback: 'none' });
    }
    return Promise.resolve();
  }

  metrics(): SpeechLatencyMetrics {
    return { ...this.metricsValue };
  }

  private sendConfigure(): void {
    this.socket?.send(
      JSON.stringify({
        type: 'Configure',
        keyterms: this.keyterms,
        thresholds: {
          eager_eot_threshold: this.config.eagerEotThreshold,
          eot_threshold: this.config.eotThreshold,
          eot_timeout_ms: this.config.eotTimeoutMs,
        },
      }),
    );
  }

  private onMessage(raw: RawData): void {
    let message: FluxMessage;
    try {
      message = JSON.parse(asText(raw)) as FluxMessage;
    } catch {
      this.fail('invalid Flux websocket message');
      return;
    }
    if (message.type === 'Error' || message.type === 'ConfigureFailure') {
      this.fail(message.description ?? message.message ?? message.type);
      return;
    }
    // Keep durable input exactly as Deepgram produced it. Math notation is a
    // user-confirmable UI candidate, never an implicit transcription rewrite.
    const transcript = message.transcript?.trim() ?? '';
    const correctionCandidate = transcript ? theoryCorrectionCandidate(transcript) : undefined;
    const event = message.event;
    if (event === 'TurnResumed') {
      this.emitTranscript({ kind: 'turn_resumed', transcript, correctionCandidate, turnIndex: message.turn_index });
      return;
    }
    if (event === 'StartOfTurn') {
      this.emitTranscript({ kind: 'turn_start', transcript, correctionCandidate, turnIndex: message.turn_index });
      return;
    }
    if (event === 'EagerEndOfTurn') {
      this.metricsValue.sttFirstPartialMs ??= Date.now() - this.startedAt;
      this.emitTranscript({ kind: 'eager_turn_end', transcript, correctionCandidate, turnIndex: message.turn_index });
      return;
    }
    if (event === 'EndOfTurn') {
      this.metricsValue.sttTurnEndMs ??= Date.now() - this.startedAt;
      this.emitTranscript({
        kind: 'final',
        transcript,
        correctionCandidate,
        turnIndex: message.turn_index,
        confidence: message.end_of_turn_confidence,
      });
      this.emitTranscript({ kind: 'turn_end', transcript, correctionCandidate, turnIndex: message.turn_index });
      return;
    }
    if (transcript) {
      this.metricsValue.sttFirstPartialMs ??= Date.now() - this.startedAt;
      this.emitTranscript({ kind: 'partial', transcript, correctionCandidate, turnIndex: message.turn_index });
    }
  }

  private emitTranscript(event: TranscriptEvent): void {
    this.sink.onTranscript?.(event);
  }

  private fail(error: unknown): void {
    this.metricsValue.providerErrors += 1;
    this.setState({ provider: 'deepgram', state: 'failed', fallback: 'none', reason: redactProviderError('deepgram', error) });
  }

  private setState(state: SpeechProviderState): void {
    this.state = state;
    this.sink.onProviderState?.(state);
  }
}
