import { CartesiaSonicTts } from './cartesia-sonic.js';
import { loadCallSpeechConfig, type CallSpeechConfig } from './config.js';
import { DeepgramFluxStt } from './deepgram-flux.js';
import { TtsPhraseQueue } from './tts-queue.js';
import type { PcmAudioFrame, SpeechEventSink, SpeechLatencyMetrics } from './types.js';

/** Structural adapter for CallRtcMedia's Int16Array frames without importing it. */
export interface RtcPcmFrame {
  data: Int16Array;
  sampleRate: number;
  channels: number;
  samplesPerChannel: number;
}

export function rtcFrameToSpeechFrame(frame: RtcPcmFrame): PcmAudioFrame {
  if (frame.channels !== 1 || frame.sampleRate !== 16_000 || frame.samplesPerChannel <= 0) {
    throw new Error('Call speech accepts only mono 16kHz RTC PCM frames.');
  }
  const pcm = Buffer.from(frame.data.buffer, frame.data.byteOffset, frame.data.byteLength);
  return { pcm, sampleRateHz: frame.sampleRate, channels: frame.channels };
}

export class CallSpeechPipeline {
  readonly stt: DeepgramFluxStt;
  readonly tts: TtsPhraseQueue;

  constructor(config: CallSpeechConfig, sink: SpeechEventSink = {}) {
    this.stt = new DeepgramFluxStt(config.deepgram, sink);
    this.tts = new TtsPhraseQueue(new CartesiaSonicTts(config.cartesia, sink), sink);
  }

  async start(): Promise<void> {
    await Promise.all([this.stt.start(), this.tts.warm()]);
  }

  pushRtcFrame(frame: RtcPcmFrame): void {
    this.stt.pushPcm(rtcFrameToSpeechFrame(frame));
  }

  updateTheoryKeyterms(keyterms: readonly string[]): void {
    this.stt.updateTheoryKeyterms(keyterms);
  }

  async speak(text: string, options?: { sentenceId?: string }): Promise<void> {
    void options; // sentence IDs remain coordinator metadata; phrase IDs are fencing tokens.
    await this.tts.speak(text);
  }

  async bargeIn(): Promise<void> {
    await this.tts.cancel('barge-in');
  }

  async close(): Promise<void> {
    await Promise.all([this.stt.close(), this.tts.close()]);
  }

  metrics(): SpeechLatencyMetrics {
    const stt = this.stt.metrics();
    const tts = this.tts.metrics();
    return {
      ...stt,
      ...tts,
      inputAudioMs: stt.inputAudioMs,
      outputAudioBytes: tts.outputAudioBytes,
      outputCharacters: tts.outputCharacters,
      providerErrors: stt.providerErrors + tts.providerErrors,
    };
  }
}

export function createCallSpeechPipelineFromEnvironment(sink: SpeechEventSink = {}): CallSpeechPipeline {
  return new CallSpeechPipeline(loadCallSpeechConfig(), sink);
}
