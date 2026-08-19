import { useState, useRef, useEffect } from 'react';
import { LayoutGrid, ChevronUp, ChevronDown } from 'lucide-react';
import type { KanbanColumn } from './KanbanBoard';

export interface ColumnPref { id: string; visible: boolean; }

export function ColumnsPicker({ prefs, onPrefsChange, columns }: {
  prefs: ColumnPref[];
  onPrefsChange: (p: ColumnPref[]) => void;
  columns: KanbanColumn[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  function toggle(id: string) {
    onPrefsChange(prefs.map(p => p.id === id ? { ...p, visible: !p.visible } : p));
  }

  function move(id: string, dir: -1 | 1) {
    const idx = prefs.findIndex(p => p.id === id);
    if (idx < 0) return;
    const swapIdx = idx + dir;
    if (swapIdx < 0 || swapIdx >= prefs.length) return;
    const next = [...prefs];
    [next[idx], next[swapIdx]] = [next[swapIdx], next[idx]];
    onPrefsChange(next);
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
          open
            ? 'bg-amber-500/20 border-amber-500/40 text-amber-400'
            : 'bg-workbench-base/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
        }`}
      >
        <LayoutGrid className="w-3.5 h-3.5" />
        Columns
      </button>
      {open && (
        <div className="absolute top-full mt-1.5 left-0 z-50 bg-workbench-base border border-neutral-800 rounded-lg shadow-xl p-1.5 min-w-[180px]">
          {prefs.map((pref, idx) => {
            const col = columns.find(c => c.id === pref.id);
            if (!col) return null;
            return (
              <div key={pref.id} className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-workbench-hover/50 group">
                <input
                  type="checkbox"
                  checked={pref.visible}
                  onChange={() => toggle(pref.id)}
                  className="w-3.5 h-3.5 accent-amber-400 cursor-pointer flex-shrink-0"
                />
                <span className="flex-1 text-xs text-neutral-300">{col.label}</span>
                <div className="flex flex-col gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                  <button
                    onClick={() => move(pref.id, -1)}
                    disabled={idx === 0}
                    className="p-0.5 rounded text-neutral-500 hover:text-neutral-300 disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    <ChevronUp className="w-3 h-3" />
                  </button>
                  {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                  <button
                    onClick={() => move(pref.id, 1)}
                    disabled={idx === prefs.length - 1}
                    className="p-0.5 rounded text-neutral-500 hover:text-neutral-300 disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    <ChevronDown className="w-3 h-3" />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
