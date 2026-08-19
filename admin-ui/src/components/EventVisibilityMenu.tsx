import { useMemo } from 'react';
import { Settings } from 'lucide-react';
import { parseTimestamp } from '../utils/time';
import { type EventPref } from './eventPrefs';
import type { ProjectMessage } from '../types/messages';

interface EventVisibilityMenuProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  messages: ProjectMessage[];
  eventPrefs: Record<string, EventPref>;
  onUpdatePref: (et: string, pref: EventPref) => void;
  onResetAll: () => void;
}

const HIGH_VOLUME_THRESHOLD = 10;

export function EventVisibilityMenu({ open, onOpenChange, messages, eventPrefs, onUpdatePref, onResetAll }: EventVisibilityMenuProps) {
  // Calculate 24-hour volume for each event type and rank event types by it. Sourced
  // from open messages + known prefs without coupling to any severity filter — the
  // popover manages prefs across all event types.
  const { eventType24hVolume, sortedEventTypes } = useMemo(() => {
    // eslint-disable-next-line react-hooks/purity -- render-time freshness check, unchanged from the original inline MessagesPanel render
    const now = Date.now();
    const twentyFourHoursMs = 24 * 60 * 60 * 1000;
    const counts = new Map<string, number>();
    for (const msg of messages) {
      const et = msg.context.event_type as string | undefined;
      if (!et) continue;
      if (msg.created_at) {
        const msgTime = parseTimestamp(msg.created_at).getTime();
        if (now - msgTime <= twentyFourHoursMs) {
          counts.set(et, (counts.get(et) ?? 0) + 1);
        }
      }
    }

    const allEts = [...new Set([
      ...Object.keys(eventPrefs),
      ...messages.filter(m => m.status === 'open')
        .flatMap(m => { const et = m.context.event_type as string | undefined; return et ? [et] : []; }),
    ])];

    const sorted = [...allEts].sort((a, b) => {
      const countA = counts.get(a) ?? 0;
      const countB = counts.get(b) ?? 0;
      return countB - countA;
    });

    return { eventType24hVolume: counts, sortedEventTypes: sorted };
  }, [messages, eventPrefs]);

  return (
    <div className="relative">
      <button
        onClick={() => onOpenChange(!open)}
        title="Event type visibility"
        className={`p-2 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-[36px] flex items-center justify-center ${open ? 'text-neutral-300 bg-workbench-panel border-neutral-700' : 'text-neutral-500 hover:text-neutral-300 border-transparent'}`}
      >
        <Settings className="w-4 h-4" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => onOpenChange(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 w-60 bg-workbench-base border border-neutral-700 rounded-lg shadow-xl p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-neutral-300">Event visibility</span>
              {Object.keys(eventPrefs).length > 0 && (
                <button onClick={onResetAll} className="text-ui-sm text-neutral-500 hover:text-red-400 transition-colors">Reset all</button>
              )}
            </div>
            {sortedEventTypes.length === 0 ? (
              <p className="text-ui-md text-neutral-600 py-1">No events loaded</p>
            ) : sortedEventTypes.map(et => {
              const pref = eventPrefs[et] ?? 'routine';
              const count = eventType24hVolume.get(et) ?? 0;
              const isHighVolume = count >= HIGH_VOLUME_THRESHOLD;
              return (
                <div key={et} className="flex items-center gap-1.5">
                  <div className="flex items-center gap-1 flex-1 min-w-0">
                    <span className="text-ui-md text-neutral-400 truncate">{et.replace(/_/g, ' ')}</span>
                    {count > 0 && (
                      <span className={`text-ui-sm shrink-0 px-1.5 py-0.5 rounded-full font-medium ${
                        isHighVolume
                          ? 'bg-orange-500/20 text-orange-400 border border-orange-500/30'
                          : 'bg-workbench-panel text-neutral-500'
                      }`}>
                        {count}
                      </span>
                    )}
                  </div>
                  {(['surfaced', 'routine', 'mute'] as EventPref[]).map(p => (
                    <button key={p} onClick={() => onUpdatePref(et, p)}
                      title={p}
                      className={`text-ui-sm px-1.5 py-0.5 rounded border transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center ${pref === p ? (p === 'mute' ? 'border-red-500/50 text-red-400 bg-red-500/10' : p === 'surfaced' ? 'border-amber-500/50 text-amber-400 bg-amber-500/10' : 'border-neutral-600 text-neutral-300 bg-workbench-panel') : 'border-transparent text-neutral-600 hover:text-neutral-400'}`}>
                      {p === 'surfaced' ? '↑' : p === 'mute' ? '✕' : '·'}
                    </button>
                  ))}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
