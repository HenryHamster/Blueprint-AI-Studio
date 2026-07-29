import type { PhraseSynthesizer } from './cartesia-sonic.js';
import { newPhraseId } from './cartesia-sonic.js';
import { splitSpeakablePhrases } from './theory-text.js';
import type { SpeechEventSink, SpeechLatencyMetrics, StreamingTts } from './types.js';

function emptyMetrics(): SpeechLatencyMetrics {
  return { inputAudioMs: 0, outputAudioBytes: 0, outputCharacters: 0, providerErrors: 0 };
}

interface QueuedPhrase {
  phraseId: string;
  text: string;
}

/**
 * Generation fencing makes interruption immediate at the queue boundary. A
 * provider may still race a final frame after cancel, but it is ignored before
 * it reaches RTC playout/captions.
 */
export class TtsPhraseQueue implements StreamingTts {
  private queue: QueuedPhrase[] = [];
  private current: { phraseId: string; controller: AbortController } | undefined;
  private generation = 0;
  private pumping: Promise<void> | undefined;
  private closed = false;
  private speakStartedAt: number | undefined;
  private readonly metricsValue = emptyMetrics();

  constructor(private readonly provider: PhraseSynthesizer, private readonly sink: SpeechEventSink = {}) {}

  /** Warm the provider before the first model sentence without starting a turn. */
  async warm(): Promise<void> {
    if (this.closed) throw new Error('TTS queue is closed.');
    await this.provider.start();
  }

  async speak(text: string): Promise<void> {
    if (this.closed) throw new Error('TTS queue is closed.');
    const phrases = splitSpeakablePhrases(text);
    this.speakStartedAt ??= Date.now();
    this.queue.push(...phrases.map((phrase) => ({ phraseId: newPhraseId(), text: phrase })));
    if (!this.pumping) this.pumping = this.pump();
    await this.pumping;
  }

  cancel(_reason = 'barge-in'): Promise<void> {
    const startedAt = Date.now();
    this.generation += 1;
    this.queue = [];
    const current = this.current;
    current?.controller.abort();
    if (current) {
      this.provider.cancel(current.phraseId);
      this.sink.onPhraseLifecycle?.({ phraseId: current.phraseId, event: 'interrupted' });
    }
    this.metricsValue.ttsQueueCancelMs = Date.now() - startedAt;
    return Promise.resolve();
  }

  async close(): Promise<void> {
    this.closed = true;
    await this.cancel('close');
    await this.pumping;
    await this.provider.close();
  }

  metrics(): SpeechLatencyMetrics {
    const provider = this.provider.metrics();
    return {
      ...provider,
      ...this.metricsValue,
      outputAudioBytes: provider.outputAudioBytes,
      outputCharacters: provider.outputCharacters,
      providerErrors: provider.providerErrors,
    };
  }

  private async pump(): Promise<void> {
    try {
      await this.provider.start();
      while (this.queue.length > 0 && !this.closed) {
        const next = this.queue.shift();
        if (!next) continue;
        const generation = this.generation;
        const controller = new AbortController();
        this.current = { phraseId: next.phraseId, controller };
        const lifecycle = { started: false };
        try {
          await this.provider.synthesize({
            phraseId: next.phraseId,
            text: next.text,
            signal: controller.signal,
            onAudio: (event) => {
              if (generation !== this.generation || controller.signal.aborted) return;
              if (!lifecycle.started) {
                lifecycle.started = true;
                this.sink.onPhraseLifecycle?.({ phraseId: next.phraseId, event: 'started' });
              }
              this.metricsValue.ttsFirstAudioMs ??= Date.now() - (this.speakStartedAt ?? Date.now());
              this.sink.onAudio?.(event);
            },
            onCaption: (event) => {
              if (generation !== this.generation || controller.signal.aborted) return;
              this.sink.onCaption?.(event);
            },
          });
          if (lifecycle.started && generation === this.generation && !controller.signal.aborted) {
            this.sink.onPhraseLifecycle?.({ phraseId: next.phraseId, event: 'completed' });
          }
        } catch (error) {
          if (!controller.signal.aborted) throw error;
        } finally {
          this.current = undefined;
        }
      }
    } finally {
      this.pumping = undefined;
      this.speakStartedAt = undefined;
    }
  }
}
