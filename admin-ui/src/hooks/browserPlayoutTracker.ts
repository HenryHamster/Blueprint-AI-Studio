export type BrowserPlayoutEvent = {
  phraseId: string;
  event: 'started' | 'completed' | 'interrupted';
  at: string;
};

type SourceEvent = BrowserPlayoutEvent['event'];

/**
 * Conservatively maps trusted source markers onto the browser's rendered
 * audio clock. It may under-report contiguous phrases, but it never treats an
 * SCTP marker alone as proof that sound reached the browser.
 */
export class BrowserPlayoutTracker {
  private readonly pending: string[] = [];
  private readonly terminal = new Map<string, Exclude<SourceEvent, 'started'>>();
  private readonly reported = new Set<string>();
  private active: string | null = null;
  private silenceSinceMs: number | null = null;
  private readonly emit: (event: BrowserPlayoutEvent) => void;

  constructor(emit: (event: BrowserPlayoutEvent) => void) {
    this.emit = emit;
  }

  source(phraseId: string, event: SourceEvent): void {
    if (!phraseId) return;
    if (event === 'started') {
      if (this.active !== phraseId && !this.pending.includes(phraseId)) this.pending.push(phraseId);
      return;
    }
    this.terminal.set(phraseId, event);
  }

  audioLevel(audible: boolean, nowMs = Date.now()): void {
    if (audible) {
      this.silenceSinceMs = null;
      if (this.active === null) {
        this.active = this.pending.shift() ?? null;
        if (this.active) this.report(this.active, 'started', nowMs);
      }
      return;
    }
    if (this.active === null) return;
    this.silenceSinceMs ??= nowMs;
    if (nowMs - this.silenceSinceMs < 120) return;
    const terminal = this.terminal.get(this.active);
    if (!terminal) return;
    this.report(this.active, terminal, nowMs);
    this.terminal.delete(this.active);
    this.active = null;
    this.silenceSinceMs = null;
  }

  private report(phraseId: string, event: BrowserPlayoutEvent['event'], nowMs: number): void {
    const key = `${phraseId}:${event}`;
    if (this.reported.has(key)) return;
    this.reported.add(key);
    this.emit({ phraseId, event, at: new Date(nowMs).toISOString() });
  }
}

export function monitorBrowserAudio(
  mediaTrack: MediaStreamTrack,
  onLevel: (audible: boolean, atMs: number) => void,
): () => void {
  const AudioContextConstructor = window.AudioContext;
  if (!AudioContextConstructor) return () => {};
  const context = new AudioContextConstructor();
  const clonedTrack = mediaTrack.clone();
  const stream = new MediaStream([clonedTrack]);
  const source = context.createMediaStreamSource(stream);
  const analyser = context.createAnalyser();
  analyser.fftSize = 512;
  const silentGain = context.createGain();
  silentGain.gain.value = 0;
  source.connect(analyser);
  analyser.connect(silentGain);
  silentGain.connect(context.destination);
  const samples = new Float32Array(analyser.fftSize);
  const timer = window.setInterval(() => {
    analyser.getFloatTimeDomainData(samples);
    let energy = 0;
    for (const sample of samples) energy += sample * sample;
    onLevel(Math.sqrt(energy / samples.length) >= 0.004, Date.now());
  }, 20);
  void context.resume().catch(() => undefined);
  return () => {
    window.clearInterval(timer);
    clonedTrack.stop();
    source.disconnect();
    analyser.disconnect();
    silentGain.disconnect();
    void context.close().catch(() => undefined);
  };
}
