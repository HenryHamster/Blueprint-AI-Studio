import { Mic, MicOff, Phone, PhoneOff, RefreshCw, Volume2 } from 'lucide-react';
import type { CallDisplayBlock, LiveCaption, StrategicCallConnectionState } from '../hooks/useStrategicCall';
import { MarkdownContent } from './MarkdownContent';

export interface StrategicCallModeProps {
  enabled: boolean;
  connectionState: StrategicCallConnectionState;
  microphoneState: 'off' | 'requesting' | 'on' | 'denied';
  audioPlaybackState: 'unknown' | 'enabled' | 'blocked';
  captions: LiveCaption[];
  displayBlocks: CallDisplayBlock[];
  error: string | null;
  onStart: () => void;
  onEnd: () => void;
  onReconnect: () => void;
  onToggleMicrophone: () => void;
  onEnableAudio: () => void;
}

function connectionCopy(state: StrategicCallConnectionState, microphone: StrategicCallModeProps['microphoneState']): string {
  if (state === 'starting') return 'Preparing live call…';
  if (state === 'connecting') return microphone === 'requesting' ? 'Requesting microphone…' : 'Connecting audio…';
  if (state === 'waiting_for_resident') return 'Audio connected · waiting for Resident…';
  if (state === 'reconnecting') return 'Reconnecting — captions will resume.';
  if (state === 'connected') {
    if (microphone === 'on') return 'Microphone on · call active';
    if (microphone === 'off') return 'Call active · microphone muted';
    return 'Call active · microphone unavailable';
  }
  if (state === 'error') return 'Call needs attention';
  return 'Voice is off · typed messages still use this conversation';
}

function DisplayBlock({ block }: { block: CallDisplayBlock }) {
  if (block.type === 'latex') {
    return <div className="text-sm"><span className="sr-only">{block.content}</span><MarkdownContent content={`$$${block.content}$$`} /></div>;
  }
  if (block.type === 'code') {
    return <pre className="overflow-x-auto rounded border border-neutral-800 bg-neutral-950 p-3 text-xs text-neutral-200"><code>{block.content}</code></pre>;
  }
  if (block.type === 'table') {
    return <pre className="overflow-x-auto whitespace-pre rounded border border-neutral-800 bg-neutral-950 p-3 text-xs text-neutral-200">{block.content}</pre>;
  }
  return <div className="rounded border border-neutral-800 bg-neutral-950/50 px-3 py-2 text-xs text-neutral-300">{block.content}</div>;
}

/** Compact, ephemeral call surface embedded above the existing composer. */
export function StrategicCallMode({
  enabled,
  connectionState,
  microphoneState,
  audioPlaybackState,
  captions,
  displayBlocks,
  error,
  onStart,
  onEnd,
  onReconnect,
  onToggleMicrophone,
  onEnableAudio,
}: StrategicCallModeProps) {
  const active = enabled && connectionState !== 'idle' && connectionState !== 'error';
  const copy = connectionCopy(connectionState, microphoneState);
  return (
    <section aria-label="Live conversation" data-testid="strategic-call-mode" className="mx-auto mb-2 w-full max-w-[940px] rounded-lg border border-cyan-900/60 bg-neutral-950/80 px-3 py-2 shadow-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <div className="flex min-w-0 items-center gap-2 text-sm text-neutral-200">
          <span className={`h-2 w-2 shrink-0 rounded-full ${active ? 'bg-cyan-400 animate-pulse' : 'bg-neutral-600'}`} aria-hidden="true" />
          <Volume2 className="h-4 w-4 shrink-0 text-cyan-300" aria-hidden="true" />
          <span className="font-medium">Live conversation</span>
          <span data-testid="strategic-call-state" className="truncate text-xs text-neutral-400">{copy}</span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {enabled ? (
            <>
              {audioPlaybackState === 'blocked' && (
                <button type="button" onClick={onEnableAudio} className="inline-flex min-h-[44px] items-center gap-1.5 rounded border border-amber-700 bg-amber-950/40 px-2.5 text-xs text-amber-100 hover:bg-amber-950/70">
                  <Volume2 className="h-3.5 w-3.5" aria-hidden="true" /> Enable audio
                </button>
              )}
              <button type="button" onClick={onToggleMicrophone} aria-label={microphoneState === 'on' ? 'Mute microphone' : 'Unmute microphone'} className="inline-flex min-h-[44px] items-center gap-1.5 rounded border border-neutral-700 px-2.5 text-xs text-neutral-200 hover:bg-neutral-800">
                {microphoneState === 'on' ? <MicOff className="h-3.5 w-3.5" aria-hidden="true" /> : <Mic className="h-3.5 w-3.5" aria-hidden="true" />}
                {microphoneState === 'on' ? 'Mute' : 'Unmute'}
              </button>
              <button type="button" onClick={onEnd} className="inline-flex min-h-[44px] items-center gap-1.5 rounded border border-rose-800 bg-rose-950/40 px-2.5 text-xs text-rose-200 hover:bg-rose-950/70">
                <PhoneOff className="h-3.5 w-3.5" aria-hidden="true" /> End call
              </button>
            </>
          ) : (
            <button type="button" onClick={onStart} disabled={connectionState === 'starting' || connectionState === 'connecting'} className="inline-flex min-h-[44px] items-center gap-1.5 rounded border border-cyan-700 bg-cyan-950/50 px-2.5 text-xs text-cyan-100 hover:bg-cyan-950 disabled:opacity-50">
              <Phone className="h-3.5 w-3.5" aria-hidden="true" /> Start call
            </button>
          )}
          {(connectionState === 'error' || connectionState === 'reconnecting') && (
            <button type="button" onClick={onReconnect} className="inline-flex min-h-[44px] items-center gap-1 rounded border border-neutral-700 px-2 text-xs text-neutral-200 hover:bg-neutral-800">
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Reconnect
            </button>
          )}
        </div>
      </div>
      {enabled && (
        <div className="mt-1.5 flex items-center gap-1.5 text-xs text-neutral-400">
          <Mic className={`h-3.5 w-3.5 ${microphoneState === 'on' ? 'text-cyan-300' : microphoneState === 'denied' ? 'text-rose-300' : ''}`} aria-hidden="true" />
          {microphoneState === 'denied' ? 'Microphone permission was denied. Keep typing or reconnect after enabling it.' : 'One participant only. Keep typing or share an exact Whiteboard revision while listening.'}
        </div>
      )}
      {!enabled && (
        <p className="mt-1.5 text-xs text-neutral-500">
          Voice audio is transcribed by Deepgram and replies are voiced by Cartesia. Blueprint saves the
          transcript in this conversation but does not record the raw call audio.
        </p>
      )}
      {error && <p role="alert" className="mt-1.5 text-xs text-rose-300">{error}</p>}
      {enabled && captions.length > 0 && (
        <div aria-live="polite" data-testid="strategic-call-captions" className="mt-2 max-h-28 space-y-1 overflow-y-auto border-t border-neutral-800 pt-2 text-sm text-neutral-200">
          {captions.map(caption => <p key={caption.id}><span className="mr-1 text-xs text-cyan-300">{caption.speaker === 'resident' ? 'Workday' : 'You'}</span>{caption.text}</p>)}
        </div>
      )}
      {enabled && displayBlocks.length > 0 && (
        <div data-testid="strategic-call-display-blocks" className="mt-2 space-y-2 border-t border-neutral-800 pt-2">
          {displayBlocks.map(block => <div key={block.id}>{block.title && <p className="mb-1 text-xs text-neutral-400">{block.title}</p>}<DisplayBlock block={block} /></div>)}
        </div>
      )}
    </section>
  );
}
