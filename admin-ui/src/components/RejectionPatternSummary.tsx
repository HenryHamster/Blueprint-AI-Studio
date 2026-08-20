import type { DigestData } from './DashboardHome';

export function RejectionPatternSummary({ digest }: { digest: DigestData | null }) {
  const rps = digest?.rejection_pattern_summary;
  if (!rps || rps.total_rejected === 0 || rps.top_notes.length === 0) return null;
  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Human Rejection Patterns <span className="text-neutral-600 font-normal normal-case tracking-normal">· {rps.total_rejected} rejected</span>
      </h2>
      <div className="flex flex-col gap-1">
        {rps.top_notes.map((note, i) => (
          <div
            key={i}
            className="flex items-start gap-2 text-xs bg-workbench-base/50 border border-neutral-800/50 px-2 py-1.5 rounded"
          >
            <span className="shrink-0 text-ui-xs font-semibold px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 tabular-nums">
              ×{note.count}
            </span>
            <span className="flex-1 text-neutral-400 font-mono text-ui-sm break-words" title={note.snippet}>
              {note.snippet.length > 160 ? note.snippet.slice(0, 160) + '…' : note.snippet}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
