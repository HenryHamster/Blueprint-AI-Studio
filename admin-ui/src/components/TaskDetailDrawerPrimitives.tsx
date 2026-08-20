import { useState, type ReactNode } from 'react';
import { Copy, Check, ChevronDown, ChevronRight } from 'lucide-react';
import type { Task } from '../types/task';
import { taskStatusTextColor } from '../utils/taskStatus';

export function CopyableId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <button
      onClick={handleCopy}
      className="flex flex-wrap items-center gap-1.5 font-mono text-sm text-neutral-200 hover:text-white transition-colors group min-h-[44px] min-w-0"
    >
      <span className="break-all min-w-0">{value}</span>
      {copied ? (
        <Check className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
      ) : (
        <Copy className="w-3.5 h-3.5 text-neutral-500 group-hover:text-neutral-300 transition-colors shrink-0" />
      )}
    </button>
  );
}

export function SectionHeader({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 mt-5 first:mt-0">{children}</h3>;
}

export function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-1.5 min-w-0">
      <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">{label}</span>
      <div className="text-sm text-neutral-200 min-w-0 flex-1">{children}</div>
    </div>
  );
}

export function DirectionsSection({ directions }: { directions: { text: string; priority?: string; status?: string }[] }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="mt-5">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-neutral-400 hover:text-neutral-200 transition-colors w-full text-left mb-2"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Directions ({directions.length})
      </button>
      {open && (
        <ul className="space-y-1.5 pl-1">
          {directions.map((d, i) => (
            <li key={i} className="flex items-start gap-2 text-sm text-neutral-200">
              <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-workbench-control shrink-0" />
              <span className="break-words min-w-0">{d.text}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const W = 40, H = 16, PAD = 1;
  const pts = values.map((v, i) => {
    const x = PAD + (i / (values.length - 1)) * (W - PAD * 2);
    const y = PAD + (1 - (v - min) / range) * (H - PAD * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const latest = values[values.length - 1];
  const pct = (latest - min) / range;
  const stroke = pct > 0.9 ? '#f87171' : pct > 0.7 ? '#fbbf24' : '#34d399';
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="inline-block align-middle">
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function DependencyItem({ dep, depTask, hasCycle, onNavigateToTask }: { dep: string; depTask?: Task; hasCycle?: boolean; onNavigateToTask?: (id: string) => void }) {
  const statusColor = depTask ? taskStatusTextColor(depTask.status) : '';
  const label = depTask ? (depTask.title.length > 40 ? depTask.title.slice(0, 40) + '…' : depTask.title) : null;
  return (
    <div className="font-mono text-sm text-neutral-300 flex items-center gap-1.5 flex-wrap">
      <ChevronRight className="w-3 h-3 text-neutral-600 shrink-0" />
      {depTask ? (<>
        {onNavigateToTask
          ? <button onClick={() => onNavigateToTask(dep)} className="text-blue-400 hover:text-blue-300 transition-colors text-left">{label}</button>
          : <span>{label}</span>}
        <span className={`text-ui-sm uppercase font-semibold ${statusColor}`}>{depTask.status}</span>
        {hasCycle && <span className="text-ui-sm font-semibold text-orange-400">Cycle detected</span>}
      </>) : (<>
        <span className="text-neutral-500 break-all min-w-0">{dep}</span>
        <span className="text-ui-sm text-neutral-600 shrink-0">(not found)</span>
      </>)}
    </div>
  );
}
