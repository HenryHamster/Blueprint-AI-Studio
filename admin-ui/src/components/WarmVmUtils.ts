import { parseTimestamp } from '../utils/time';

export function ageSecs(iso: string | null): number | null {
  if (!iso) return null;
  // Backend emits naive UTC datetimes; parseTimestamp() appends 'Z' so the
  // value is interpreted as UTC, not local time.
  const t = parseTimestamp(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

export function fmtAge(secs: number | null): string {
  if (secs === null) return '—';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  return `${Math.round(secs / 3600)}h`;
}

export function fmtSetupSecs(s: number | null): string {
  if (s === null || s === undefined) return '—';
  if (s < 1) return `<1s`;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.round(s / 60)}m`;
}

export function shortVm(id: string): string {
  const tail = id.split('-').slice(-1)[0] || id;
  return tail.length > 12 ? tail.slice(0, 12) : tail;
}

export function fmtUsd(v: number | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—';
  if (v === 0) return '$0';
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}
