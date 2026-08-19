import { AlertTriangle, Infinity as InfinityIcon, LoaderCircle, MessageSquare, Play, Server, Square } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { StrategicResidentRunnerOption } from '../strategicStateApi';
import { WORKDAY_STATE_PRESENTATION, type ConversationWorkdayStatus } from '../utils/conversationWorkdayStatus';

export type StrategicInteractionMode = 'chat' | 'continuous';

export interface StrategicSessionControlProps {
  currentMode?: StrategicInteractionMode | null;
  mode?: StrategicInteractionMode | null;
  saving?: boolean;
  sessionActive?: boolean;
  sessionState?: string | null;
  // The single reconciled status readout — the same ConversationWorkdayStatus
  // the utility bar renders. This is the only source for the visible status
  // label/detail; sessionState/sessionActive above are Start/Stop button
  // affordance inputs only, not a second display vocabulary.
  workdayStatus?: ConversationWorkdayStatus | null;
  sessionBusy?: boolean;
  sessionRunners?: StrategicResidentRunnerOption[] | null;
  sessionSelectedRunnerId?: string | null;
  onSelectRunner?: (runnerId: string | null) => void;
  onStartSession?: () => void;
  onStopSession?: () => void;
  onModeChange?: (mode: StrategicInteractionMode) => void;
  density?: 'default' | 'compact';
  className?: string;
}

const MODES: Array<{ value: StrategicInteractionMode; label: string; Icon: LucideIcon }> = [
  { value: 'chat', label: 'Chat', Icon: MessageSquare },
  { value: 'continuous', label: 'Workday', Icon: InfinityIcon },
];

const FALLBACK_PRESENTATION = WORKDAY_STATE_PRESENTATION.ready;
const STUCK_STATES = new Set(['unavailable', 'needs_restart']);

function runnerLabel(runner: StrategicResidentRunnerOption): string {
  const name = runner.name || runner.hostname || runner.id.slice(0, 8);
  if (!runner.online) return `${name} — offline`;
  if (!runner.agent_ready) return `${name} — agent unavailable`;
  if (!runner.eligible) return `${name} — capability mismatch`;
  return name;
}

export function StrategicSessionControl({
  currentMode,
  mode,
  saving = false,
  sessionActive = false,
  sessionState,
  workdayStatus,
  sessionBusy = false,
  sessionRunners,
  sessionSelectedRunnerId,
  onSelectRunner,
  onStartSession,
  onStopSession,
  onModeChange,
  density = 'compact',
  className = '',
}: StrategicSessionControlProps) {
  const selectedMode = currentMode ?? mode ?? 'chat';
  const compact = density === 'compact';
  const normalizedSessionState = sessionState?.trim().toLowerCase() || null;
  // The lifecycle state is authoritative whenever the newer API supplies it.
  // Fall back to the legacy active snapshot only for older responses where
  // the state field is absent.
  const active = normalizedSessionState
    ? normalizedSessionState === 'active'
    : sessionActive;
  const wedged = normalizedSessionState === 'blocked' || normalizedSessionState === 'error';
  // Keep a live blob stoppable even when its lifecycle label is not Active.
  const canStop = active || sessionActive || normalizedSessionState === 'starting' || normalizedSessionState === 'stopping' || wedged;
  const presentation = workdayStatus ? WORKDAY_STATE_PRESENTATION[workdayStatus.state] : FALLBACK_PRESENTATION;
  const statusLabel = workdayStatus?.label ?? 'Ready';
  const statusDetail = workdayStatus?.detail ?? null;
  const statusTitle = workdayStatus?.title ?? statusLabel;
  const spinning = sessionBusy || saving || (workdayStatus ? ['starting', 'stopping', 'waiting', 'retrying'].includes(workdayStatus.state) : false);
  const stuck = STUCK_STATES.has(workdayStatus?.state ?? '') || wedged;
  const runners = sessionRunners ?? [];

  return (
    <div className={`flex min-w-0 items-center justify-end gap-1 ${className}`} data-testid="strategic-session-control">
      <div className="flex rounded border border-neutral-800 bg-workbench-shell/80 p-0.5" role="group" aria-label="Strategic interaction mode">
        {MODES.map(({ value, label, Icon }) => {
          const selected = selectedMode === value;
          return (
            <button
              key={value}
              type="button"
              disabled={saving || !onModeChange}
              aria-pressed={selected}
              aria-label={`Switch Strategic mode to ${label}`}
              title={`Strategic mode: ${label}`}
              onClick={() => { if (!selected) onModeChange?.(value); }}
              className={`${compact ? 'h-11 w-11 sm:h-7 sm:w-7' : 'min-h-9 px-2'} inline-flex items-center justify-center gap-1.5 rounded text-xs font-semibold transition-colors disabled:opacity-50 ${selected ? 'bg-workbench-panel text-neutral-100' : 'text-neutral-400 hover:bg-workbench-hover hover:text-neutral-100'}`}
            >
              <Icon className="h-3.5 w-3.5" aria-hidden="true" />
              <span className={compact ? 'sr-only' : undefined}>{label}</span>
            </button>
          );
        })}
      </div>

      {selectedMode === 'continuous' && (
        <>
          <span
            className={`hidden min-h-7 min-w-0 max-w-64 items-center gap-1.5 rounded border px-2 text-xs sm:inline-flex ${presentation.tone}`}
            role="status"
            aria-label="Workday status"
            title={statusTitle}
            data-workday-state={workdayStatus?.state ?? 'ready'}
          >
            {spinning ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : stuck ? <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" /> : <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />}
            <span className="truncate">{statusDetail ? `${statusLabel} · ${statusDetail}` : statusLabel}</span>
          </span>

          {runners.length > 0 && onSelectRunner && (
            <label className="hidden min-h-7 items-center gap-1 rounded border border-neutral-800 bg-workbench-shell/80 px-1.5 sm:inline-flex" title="Workday device">
              <Server className="h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
              <span className="sr-only">Workday device</span>
              <select
                value={sessionSelectedRunnerId ?? ''}
                disabled={sessionBusy || active}
                onChange={event => onSelectRunner(event.target.value || null)}
                className="max-w-44 bg-transparent text-xs text-neutral-300 outline-none disabled:opacity-60"
              >
                <option value="">Auto-pick</option>
                {runners.map(runner => <option key={runner.id} value={runner.id}>{runnerLabel(runner)}</option>)}
              </select>
            </label>
          )}

          {canStop ? (
            <button type="button" onClick={onStopSession} disabled={sessionBusy || !onStopSession} className="inline-flex h-7 min-h-[44px] min-w-[44px] items-center gap-1 rounded border border-red-500/30 bg-red-500/10 px-2 text-xs font-semibold text-red-300 disabled:opacity-50" title={wedged ? 'Stop Workday session before restarting' : 'Stop Workday session'}>
              {sessionBusy ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Square className="h-3.5 w-3.5" aria-hidden="true" />}
              <span className="hidden sm:inline">Stop</span>
            </button>
          ) : (
            <button type="button" onClick={onStartSession} disabled={sessionBusy || !onStartSession} className="inline-flex h-7 min-h-[44px] min-w-[44px] items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/10 px-2 text-xs font-semibold text-emerald-300 disabled:opacity-50" title="Start Workday session">
              {sessionBusy ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Play className="h-3.5 w-3.5" aria-hidden="true" />}
              <span className="hidden sm:inline">Start</span>
            </button>
          )}
        </>
      )}
    </div>
  );
}

export default StrategicSessionControl;
