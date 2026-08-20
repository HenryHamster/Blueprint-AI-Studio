/**
 * Live, EPHEMERAL rendering of the strategist's in-flight chat turn.
 *
 * This is the last mile of the incremental delivery path: agent WS deltas →
 * `strategic_session` broadcast → `useLogViewer` transcript buffer →
 * `strategicSessionLive` derivations → here. It renders two things and only
 * these two:
 *
 *   (a) the streaming reply preview bubble, with reasoning runs visually set
 *       apart from reply runs, and
 *   (b) the ephemeral activity strip (commands / tool calls / sub-agents) for
 *       the in-flight turn.
 *
 * Nothing here is a message. It is deliberately NOT a `MessageBubble`: it has
 * no id, no selection checkbox, no reply/resolve/convert affordance, and no
 * context menu, so the operator can never act on a preview as if it were a
 * durable row. It is also strictly additive over the authenticated message
 * poll: once the durable reply lands, the derivations return null/empty and
 * this component renders nothing, leaving exactly the message set the poll
 * alone would have produced.
 */
import { LoaderCircle } from 'lucide-react';
import type {
  StrategicLiveActivity,
  StrategicStreamingTurn,
} from '../utils/strategicSessionLive';

export interface StrategicStreamingPreviewProps {
  /** The in-flight streamed reply, or null when nothing is streaming. */
  turn: StrategicStreamingTurn | null;
  /** The in-flight ephemeral activity feed. */
  activity: StrategicLiveActivity;
}

/** Activity kinds render with a short, stable prefix so the strip reads as a
 *  log of what the strategist is doing rather than as prose. */
const ACTIVITY_KIND_LABEL: Record<string, string> = {
  command: 'run',
  tool: 'tool',
  tool_result: 'result',
  file: 'edit',
  read: 'read',
  subagent: 'agent',
  action: 'action',
};

export function StrategicStreamingPreview({ turn, activity }: StrategicStreamingPreviewProps) {
  const segments = turn?.segments ?? [];
  const activityItems = activity.items;
  // Nothing in flight → render nothing at all, so the thread is byte-identical
  // to the poll-only rendering.
  if (segments.length === 0 && activityItems.length === 0) return null;

  return (
    <div
      className="px-2 md:px-2.5"
      data-testid="strategic-streaming-preview"
      data-streaming={segments.length > 0 ? 'true' : 'false'}
    >
      <div className="mx-auto w-full max-w-[940px] py-1.5">
        {activityItems.length > 0 && (
          <ul
            className="mb-1.5 flex flex-col gap-0.5 rounded-md border border-neutral-800/80 bg-workbench-shell/50 px-2.5 py-1.5"
            data-testid="strategic-live-activity"
            aria-label="Strategic live activity"
          >
            {activityItems.map(item => (
              <li
                key={item.id}
                className="flex min-w-0 items-center gap-1.5 text-ui-sm text-neutral-500"
                data-testid="strategic-live-activity-item"
                data-activity-kind={item.kind}
                data-in-flight={item.inFlight ? 'true' : undefined}
              >
                {item.inFlight
                  ? <LoaderCircle className="h-3 w-3 shrink-0 animate-spin text-neutral-400" aria-hidden="true" />
                  : <span className="h-1 w-1 shrink-0 rounded-full bg-workbench-layer" aria-hidden="true" />}
                <span className="shrink-0 font-mono text-ui-sm uppercase tracking-wide text-neutral-600">
                  {ACTIVITY_KIND_LABEL[item.kind] ?? item.kind}
                </span>
                <span className="truncate">{item.text}</span>
              </li>
            ))}
          </ul>
        )}

        {segments.length > 0 && (
          <div
            className="max-w-[85%] rounded-lg border border-dashed border-neutral-700 bg-workbench-shell/40 px-3 py-2"
            data-testid="strategic-streaming-bubble"
            role="status"
            aria-live="polite"
            aria-label="Strategist reply in progress"
          >
            <div className="mb-1 flex items-center gap-1.5 text-ui-sm font-semibold uppercase tracking-wide text-neutral-500">
              <LoaderCircle className="h-3 w-3 animate-spin" aria-hidden="true" />
              <span>Strategic is replying…</span>
            </div>
            {segments.map((segment, index) => (
              <p
                // Segments are positional runs of one continuous stream; index is
                // their identity and they only ever grow at the tail.
                key={`${turn?.key ?? 'turn'}:${index}`}
                data-testid={segment.kind === 'reasoning'
                  ? 'strategic-streaming-reasoning'
                  : 'strategic-streaming-reply'}
                className={segment.kind === 'reasoning'
                  ? 'mt-1 whitespace-pre-wrap border-l-2 border-neutral-800 pl-2 text-ui-sm italic text-neutral-500'
                  : 'mt-1 whitespace-pre-wrap text-ui-sm text-neutral-200'}
              >
                {segment.text}
              </p>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default StrategicStreamingPreview;
