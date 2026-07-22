/**
 * Presentational pieces for the "Workday" LIVE turn: the streaming
 * reply preview bubble and the ephemeral activity strip. Split out of
 * MessagesPanel to keep that file under its size cap. Pure derivations live in
 * utils/strategicSessionLive.ts.
 */
import { useState, useEffect } from 'react';
import type {
  StrategicActivityItem,
  StrategicTurnSegment,
} from '../utils/strategicSessionLive';

// Compact glyph per activity kind for the strip (monospace, muted).
const STRATEGIC_ACTIVITY_GLYPH: Record<string, string> = {
  command: '$',
  file: '✎',
  read: '☰',
  subagent: '⑃',
  tool_result: '↳',
  tool: '⚙',
  action: '★',
};

/** Live "typing"/streaming preview bubble for the strategist's in-flight
 *  turn. Reasoning (train-of-thought) is rendered in a muted, clearly-labelled
 *  block set apart from the primary reply so the two are never confused. Text uses
 *  `whitespace-pre-wrap break-words`: the fragment's own newlines survive and prose
 *  wraps word-wise (never mid-word via `break-all`), while long unbroken tokens
 *  still wrap inside the column. */
export function StrategicStreamingBubble({ segments }: { segments: StrategicTurnSegment[] }) {
  const reasoningText = segments.filter(s => s.kind === 'reasoning').map(s => s.text).join('\n').trim();
  const replyText = segments.filter(s => s.kind === 'reply').map(s => s.text).join('\n').trim();
  return (
    <div
      data-testid="strategic-streaming-bubble"
      className="group flex scroll-mt-24 justify-start rounded-md"
    >
      <div className="flex w-full min-w-0 max-w-[92%] flex-col items-start md:max-w-[76%]">
        <div className="mb-0.5 flex items-center gap-1.5 px-0.5 text-ui-md text-neutral-500">
          <span className="font-medium text-neutral-400">Strategic</span>
          <span aria-hidden="true">·</span>
          <span className="inline-flex items-center gap-1">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" aria-hidden="true" />
            responding…
          </span>
        </div>
        <div className="flex w-full min-w-0 flex-col gap-1.5 rounded-md border border-transparent bg-transparent px-0.5 py-0.5">
          {reasoningText && (
            <div
              data-testid="strategic-streaming-reasoning"
              className="min-w-0 rounded-md border-l-2 border-neutral-700 bg-neutral-900/40 px-2 py-1"
            >
              <div className="mb-0.5 text-ui-sm font-medium uppercase tracking-wide text-neutral-500">
                Reasoning
              </div>
              <div className="whitespace-pre-wrap break-words min-w-0 text-sm italic leading-relaxed text-neutral-500">
                {reasoningText}
              </div>
            </div>
          )}
          {replyText && (
            // Intentionally raw (plain text, not MarkdownContent) while streaming:
            // the fragment arrives in chunks that may split mid-delimiter, so passing
            // a partial string through remark-math would yield broken KaTeX renders.
            // The completed turn is re-rendered via MessageBubble → MarkdownContent
            // once the stream ends and the message is persisted.
            <div
              data-testid="strategic-streaming-reply"
              className="whitespace-pre-wrap break-words min-w-0 text-sm leading-relaxed text-neutral-100"
            >
              {replyText}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Ticking elapsed-seconds counter for an in-flight activity item. When `endMs` is
 * provided the counter FREEZES at `endMs - startMs` and no interval runs — this is
 * what keeps a finished / dead command from ticking forever: the caller passes an
 * end timestamp the moment the command completes or the session dies. While `endMs`
 * is undefined it ticks live against the wall clock.
 */
function InFlightCounter({ startMs, endMs }: { startMs?: number; endMs?: number }) {
  // Capture origin once on first render; never re-initialized so the counter is monotonic.
  const [origin] = useState<number>(() => startMs ?? Date.now());
  const [nowTick, setNowTick] = useState<number>(() => Date.now());
  const frozen = endMs != null;

  useEffect(() => {
    if (frozen) return; // frozen: never start an interval, so it can never tick forever.
    const id = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [frozen]);

  const reference = frozen ? (endMs as number) : nowTick;
  const elapsed = Math.max(0, Math.floor((reference - origin) / 1000));
  return <span className="shrink-0 text-neutral-500"> running… ({elapsed}s)</span>;
}

/**
 * The LIVE, ephemeral activity strip: a compact scrolling feed of the commands /
 * tools the strategist is running for the current turn. Cleared the
 * moment the turn ends (`done` / durable reply lands) — it never becomes part of
 * the durable transcript.
 */
export function StrategicActivityStrip({
  items,
  latestLifecycle,
}: {
  items: StrategicActivityItem[];
  latestLifecycle?: 'responding' | 'working' | 'done' | null;
}) {
  return (
    <div
      data-testid="strategic-activity-strip"
      className="group flex scroll-mt-24 justify-start rounded-md"
    >
      <div className="flex w-full min-w-0 max-w-[92%] flex-col items-start md:max-w-[76%]">
        <div className="mb-0.5 flex items-center gap-1.5 px-0.5 text-ui-md text-neutral-500">
          <span className="inline-flex items-center gap-1">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" aria-hidden="true" />
            <span className="font-medium text-neutral-400">Workday's on it…</span>
          </span>
        </div>
        {items.length > 0 && (
          <div
            className="max-h-28 w-full overflow-y-auto rounded-md border border-neutral-800 bg-neutral-950/60 px-2 py-1.5 font-mono text-ui-sm leading-relaxed text-neutral-400 custom-scrollbar"
            role="log"
            aria-live="polite"
            aria-label="Workday live activity"
          >
            {items.map((item, idx) => (
              <div
                key={item.id}
                className={`flex min-w-0 items-baseline gap-1.5 truncate ${idx === items.length - 1 ? 'text-neutral-200' : ''}`}
              >
                <span className="shrink-0 text-cyan-500/70" aria-hidden="true">
                  {STRATEGIC_ACTIVITY_GLYPH[item.kind] ?? '·'}
                </span>
                <span className="min-w-0 truncate">{item.text}</span>
                {item.inFlight && latestLifecycle !== 'done' && (
                  <InFlightCounter startMs={item.ts} />
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
