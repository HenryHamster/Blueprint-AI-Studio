export function PhaseChip({ label, duration, tone }: { label: string; duration: string; tone?: 'amber' }) {
  const isSpecial = duration === 'unknown' || duration === 'pending';
  let durationClass: string;
  if (tone === 'amber') {
    durationClass = 'text-amber-500';
  } else if (isSpecial) {
    durationClass = 'text-neutral-700 italic';
  } else {
    durationClass = 'text-neutral-400';
  }
  return (
    <span className="flex items-center gap-1">
      <span className="text-neutral-600">{label}</span>
      <span className={durationClass}>{duration}</span>
    </span>
  );
}
