/**
 * Presentational pieces for the resident "Workday" LIVE turn: the streaming
 * reply preview bubble and the ephemeral activity strip. Split out of
 * MessagesPanel to keep that file under its size cap. Pure derivations live in
 * utils/strategicSessionLive.ts.
 */
import type { StrategicActivityItem } from '../utils/strategicSessionLive';

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

/** Live "typing"/streaming preview bubble for the resident strategist's in-flight turn. */
export function StrategicStreamingBubble({ text }: { text: string }) {
  return (
    <div
      data-testid="strategic-streaming-bubble"
      className="group flex scroll-mt-24 justify-start rounded-md"
    >
      <div className="flex w-full max-w-[92%] flex-col items-start md:max-w-[76%]">
        <div className="mb-0.5 flex items-center gap-1.5 px-0.5 text-ui-md text-neutral-500">
          <span className="font-medium text-neutral-400">Strategic</span>
          <span aria-hidden="true">·</span>
          <span className="inline-flex items-center gap-1">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" aria-hidden="true" />
            responding…
          </span>
        </div>
        <div className="rounded-md border border-transparent bg-transparent px-0.5 py-0.5">
          <div className="whitespace-pre-wrap text-sm leading-relaxed text-neutral-100">{text}</div>
        </div>
      </div>
    </div>
  );
}

/**
 * The LIVE, ephemeral activity strip: a compact scrolling feed of the commands /
 * tools the resident strategist is running for the current turn. Cleared the
 * moment the turn ends (`done` / durable reply lands) — it never becomes part of
 * the durable transcript.
 */
export function StrategicActivityStrip({ items }: { items: StrategicActivityItem[] }) {
  return (
    <div
      data-testid="strategic-activity-strip"
      className="group flex scroll-mt-24 justify-start rounded-md"
    >
      <div className="flex w-full max-w-[92%] flex-col items-start md:max-w-[76%]">
        <div className="mb-0.5 flex items-center gap-1.5 px-0.5 text-ui-md text-neutral-500">
          <span className="inline-flex items-center gap-1">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" aria-hidden="true" />
            <span className="font-medium text-neutral-400">Workday’s on it…</span>
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
                className={`flex items-baseline gap-1.5 truncate ${idx === items.length - 1 ? 'text-neutral-200' : ''}`}
              >
                <span className="shrink-0 text-cyan-500/70" aria-hidden="true">
                  {STRATEGIC_ACTIVITY_GLYPH[item.kind] ?? '·'}
                </span>
                <span className="truncate">{item.text}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
