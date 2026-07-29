/**
 * Coordinator-owned speech boundary.  These types intentionally do not expose
 * provider credentials or Fast Resident process configuration.
 */
export type SpeechProviderName = 'deepgram' | 'cartesia';

export interface PcmAudioFrame {
  /** Signed 16-bit little-endian PCM. */
  pcm: Buffer;
  sampleRateHz: number;
  channels: number;
  sequence?: number;
  capturedAtMs?: number;
}

export interface TranscriptEvent {
  kind: 'turn_start' | 'partial' | 'eager_turn_end' | 'final' | 'turn_end' | 'turn_resumed';
  /** Canonical provider transcript. This is the only text eligible for durable commit. */
  transcript: string;
  /** A non-destructive, user-confirmable notation suggestion for the transcript UI. */
  correctionCandidate?: { text: string; reason: 'theory_notation'; requiresConfirmation: true };
  turnIndex?: number;
  confidence?: number;
  providerTimestampMs?: number;
}

export interface TheoryCaptionEvent {
  kind: 'caption';
  phraseId: string;
  words: string[];
  startMs: number[];
  endMs: number[];
}

export interface SpeechAudioEvent {
  kind: 'audio';
  phraseId: string;
  pcm: Buffer;
  sampleRateHz: number;
  channels: number;
}

export interface SpeechPhraseEvent {
  phraseId: string;
  event: 'started' | 'completed' | 'interrupted';
}

export interface SpeechProviderState {
  provider: SpeechProviderName;
  state: 'connecting' | 'ready' | 'failed' | 'closed';
  /** Deliberately contains no provider response body, which may echo secrets. */
  reason?: string;
  fallback: 'none';
}

export interface SpeechLatencyMetrics {
  sttFirstPartialMs?: number;
  sttTurnEndMs?: number;
  ttsFirstAudioMs?: number;
  ttsQueueCancelMs?: number;
  inputAudioMs: number;
  outputAudioBytes: number;
  outputCharacters: number;
  providerErrors: number;
}

export interface SpeechEventSink {
  onTranscript?(event: TranscriptEvent): void;
  onAudio?(event: SpeechAudioEvent): void;
  onCaption?(event: TheoryCaptionEvent): void;
  onPhraseLifecycle?(event: SpeechPhraseEvent): void;
  onProviderState?(event: SpeechProviderState): void;
}

export interface StreamingStt {
  start(): Promise<void>;
  pushPcm(frame: PcmAudioFrame): void;
  updateTheoryKeyterms(keyterms: readonly string[]): void;
  close(): Promise<void>;
  metrics(): SpeechLatencyMetrics;
}

export interface StreamingTts {
  speak(text: string, options?: { sentenceId?: string }): Promise<void>;
  cancel(reason?: string): Promise<void>;
  close(): Promise<void>;
  metrics(): SpeechLatencyMetrics;
}

export interface SpeechPipeline extends StreamingStt, StreamingTts {}
