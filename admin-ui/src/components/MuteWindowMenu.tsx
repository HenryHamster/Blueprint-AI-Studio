import { useState } from 'react';
import { BellOff } from 'lucide-react';
import { type MuteWindow, formatMuteCountdown } from './muteWindow';

interface MuteWindowMenuProps {
  muteActive: boolean;
  muteWindow: MuteWindow | null;
  // Persistence is owned by the parent: arm/extend pass the new window, clear passes null.
  onSetMuteWindow: (win: MuteWindow | null) => void;
}

const ARM_OPTIONS: [string, number][] = [
  ['1 hour', 3600000],
  ['4 hours', 14400000],
  ['8 hours', 28800000],
];

const EXTEND_OPTIONS: [string, number][] = [
  ['Extend +1h', 3600000],
  ['Extend +4h', 14400000],
  ['Extend +8h', 28800000],
];

export function MuteWindowMenu({ muteActive, muteWindow, onSetMuteWindow }: MuteWindowMenuProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(v => !v)}
        title={muteActive && muteWindow ? `Muted · ${formatMuteCountdown(muteWindow.expiresAt)}` : 'Mute routine ops temporarily'}
        className={`px-2 py-1 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center text-xs font-semibold ${muteActive ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'text-neutral-500 hover:text-neutral-300 border-transparent'}`}
      >
        {muteActive && muteWindow ? `🔕 ${formatMuteCountdown(muteWindow.expiresAt)}` : <BellOff className="w-4 h-4" />}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 w-44 bg-workbench-base border border-neutral-700 rounded-lg shadow-xl p-2 space-y-0.5">
            {muteActive && muteWindow ? (
              <>
                <p className="text-ui-sm text-neutral-500 px-2 py-1">Mute active</p>
                {EXTEND_OPTIONS.map(([label, ms]) => (
                  <button key={label} onClick={() => {
                    onSetMuteWindow({ expiresAt: muteWindow.expiresAt + ms });
                    setOpen(false);
                  }} className="w-full text-left text-xs px-2 py-2 rounded text-neutral-300 hover:bg-workbench-hover transition-colors min-h-[44px] md:min-h-auto flex items-center">{label}</button>
                ))}
                <div className="border-t border-neutral-800 my-1" />
                <button onClick={() => {
                  onSetMuteWindow(null);
                  setOpen(false);
                }} className="w-full text-left text-xs px-2 py-2 rounded text-amber-400 hover:bg-amber-500/10 transition-colors min-h-[44px] md:min-h-auto flex items-center">End mute early</button>
              </>
            ) : (
              <>
                <p className="text-ui-sm text-neutral-500 px-2 py-1">Mute routine ops for</p>
                {ARM_OPTIONS.map(([label, ms]) => (
                  <button key={label} onClick={() => {
                    onSetMuteWindow({ expiresAt: Date.now() + ms });
                    setOpen(false);
                  }} className="w-full text-left text-xs px-2 py-2 rounded text-neutral-300 hover:bg-workbench-hover transition-colors min-h-[44px] md:min-h-auto flex items-center">{label}</button>
                ))}
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
