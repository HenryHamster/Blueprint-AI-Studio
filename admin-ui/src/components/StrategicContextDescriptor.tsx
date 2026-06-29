import type { StrategicContextDescriptorModel } from '../utils/strategicContextDescriptor';

function toneClass(tone: StrategicContextDescriptorModel['contextTone']): string {
  if (tone === 'building') return 'bg-cyan-400';
  if (tone === 'stale') return 'bg-amber-400';
  if (tone === 'ready') return 'bg-emerald-400';
  return 'bg-neutral-500';
}

export function StrategicContextDescriptor({
  model,
  variant = 'composer',
}: {
  model: StrategicContextDescriptorModel;
  variant?: 'composer' | 'receipt';
}) {
  const visibleRefs = model.refs.slice(0, 2);
  const hiddenRefCount = Math.max(0, model.refCount - visibleRefs.length);
  const compact = variant === 'receipt';
  return (
    <div
      data-testid={compact ? 'strategic-context-receipt' : 'strategic-context-descriptor'}
      title={model.capturedAt ? `Captured ${model.capturedAt}` : undefined}
      className={compact
        ? 'flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-1 text-ui-md leading-snug text-neutral-500'
        : 'mx-auto mb-2 flex w-full max-w-4xl flex-wrap items-center gap-x-2 gap-y-1 text-ui-md leading-snug text-neutral-500'
      }
    >
      <span className="inline-flex min-w-0 items-center gap-1.5 font-medium text-neutral-400">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${toneClass(model.contextTone)}`} />
        {compact ? 'Sent with context' : 'Context'}
      </span>
      <span aria-hidden="true" className="text-neutral-700">/</span>
      <span>{model.modeLabel}</span>
      {model.surfaceLabel !== 'Strategic' && (
        <>
          <span aria-hidden="true" className="text-neutral-700">/</span>
          <span>{model.surfaceLabel}</span>
        </>
      )}
      {model.contextLabel && (
        <>
          <span aria-hidden="true" className="text-neutral-700">/</span>
          <span className={model.contextTone === 'stale' ? 'text-amber-300' : model.contextTone === 'building' ? 'text-cyan-300' : 'text-neutral-400'}>
            {model.contextLabel}
          </span>
        </>
      )}
      {visibleRefs.map(ref => (
        <span
          key={ref.id}
          className={compact
            ? 'min-w-0 max-w-full truncate text-neutral-400'
            : 'min-w-0 max-w-full rounded-md border border-neutral-800 bg-neutral-900/70 px-1.5 py-0.5 text-neutral-300'
          }
        >
          {ref.label}
          {ref.status && !compact && <span className="ml-1 text-neutral-600">{ref.status}</span>}
        </span>
      ))}
      {hiddenRefCount > 0 && (
        <span className="text-neutral-600">+{hiddenRefCount}</span>
      )}
    </div>
  );
}
