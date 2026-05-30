import { useState, useEffect, useRef } from 'react';
import { ChevronDown, Check } from 'lucide-react';
import { TYPE_CONFIG } from './MessageBubble';

const MSG_TYPE_OPTIONS = [
  { label: 'Feedback', value: 'FEEDBACK' },
  { label: 'Question', value: 'QUESTION' },
  { label: 'Bug Report', value: 'BUG_REPORT' },
  { label: 'Blocker', value: 'BLOCKER' },
  { label: 'Note', value: 'NOTE' },
];

export function MessageTypeDropdown({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  const current = MSG_TYPE_OPTIONS.find(o => o.value === value);
  const config = TYPE_CONFIG[value] || TYPE_CONFIG.NOTE;

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen(v => !v)}
        className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium transition-all border min-h-[44px] ${config.color} bg-neutral-900/50 border-neutral-800/50 hover:border-neutral-700`}
      >
        <span>{current?.label ?? value}</span>
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute bottom-full mb-1 left-0 z-50 min-w-[140px] bg-neutral-900 border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
          {MSG_TYPE_OPTIONS.map(opt => {
            const optConfig = TYPE_CONFIG[opt.value] || TYPE_CONFIG.NOTE;
            return (
              <button
                key={opt.value}
                onClick={() => { onChange(opt.value); setOpen(false); }}
                className={`w-full flex items-center justify-between px-3 py-2.5 text-xs hover:bg-neutral-800 transition-colors min-h-[44px] ${
                  opt.value === value ? 'text-neutral-100' : optConfig.color
                }`}
              >
                <span>{opt.label}</span>
                {opt.value === value && <Check className="w-3 h-3 text-neutral-400" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
