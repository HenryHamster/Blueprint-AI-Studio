import type { StrategicContextDescriptorModel } from '../utils/strategicContextDescriptor';

function toneClass(tone: StrategicContextDescriptorModel['contextTone']): string {
  if (tone === 'building') return 'bg-cyan-400';
  if (tone === 'stale') return 'bg-amber-400';
  if (tone === 'ready') return 'bg-emerald-400';
  return 'bg-workbench-control';
}

function refKindLabel(kind: string): string {
  if (kind === 'knowledge_document') return 'knowledge';
  if (kind === 'strategic_cycle') return 'cycle';
  return kind.replace(/_/g, ' ');
}

export function StrategicContextDescriptor({
  model,
  variant = 'composer',
  onOpen,
}: {
  model: StrategicContextDescriptorModel;
  variant?: 'composer' | 'receipt';
  onOpen?: () => void;
}) {
  const visibleRefs = model.refs.slice(0, 2);
  const hiddenRefCount = Math.max(0, model.refCount - visibleRefs.length);
  const compact = variant === 'receipt';
  const interactive = Boolean(onOpen) && !compact;
  const primaryLabel = compact ? (model.receiptLabel ?? 'Context attached') : 'Context';
  const showMode = !compact || (model.modeLabel !== 'Chat' && primaryLabel !== `${model.modeLabel} context`);
  const showSurface = model.surfaceLabel !== 'Strategic' && (!compact || primaryLabel !== `${model.surfaceLabel} context`);
  const showContextLabel = Boolean(model.contextLabel) && (!compact || model.contextLabel !== primaryLabel);
  const packageLabel = !compact && model.packageTitle ? model.packageTitle : null;
  const title = interactive
    ? 'Open Strategic context'
    : model.capturedAt
      ? `Captured ${model.capturedAt}`
      : undefined;
  const className = compact
    ? 'flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-1 text-ui-md leading-snug text-neutral-500'
    : 'mx-auto mb-1.5 flex w-full max-w-[940px] flex-wrap items-center gap-x-2 gap-y-1 rounded border border-transparent px-1 py-0.5 text-ui-md leading-snug text-neutral-500';
  const testId = compact ? 'strategic-context-receipt' : 'strategic-context-descriptor';
  const content = (
    <>
      <span className="inline-flex min-w-0 items-center gap-1.5 font-medium text-neutral-400">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${toneClass(model.contextTone)}`} />
        {primaryLabel}
      </span>
      {packageLabel && (
        <span className="min-w-0 max-w-full truncate text-neutral-300">{packageLabel}</span>
      )}
      {showMode && (
        <>
          <span aria-hidden="true" className="text-neutral-700">·</span>
          <span>{model.modeLabel}</span>
        </>
      )}
      {showSurface && (
        <>
          <span aria-hidden="true" className="text-neutral-700">·</span>
          <span>{model.surfaceLabel}</span>
        </>
      )}
      {showContextLabel && (
        <>
          <span aria-hidden="true" className="text-neutral-700">·</span>
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
            : 'inline-flex min-w-0 max-w-full items-center gap-1.5 rounded border border-neutral-800/70 bg-workbench-shell/50 px-1.5 py-0.5 text-neutral-300'
          }
          title={ref.summary ?? undefined}
        >
          {!compact && <span className="shrink-0 text-neutral-600">{refKindLabel(ref.kind)}</span>}
          <span className="min-w-0 truncate">{ref.label}</span>
          {ref.status && !compact && <span className="shrink-0 text-neutral-600">{ref.status}</span>}
        </span>
      ))}
      {hiddenRefCount > 0 && (
        <span className="text-neutral-600">+{hiddenRefCount}</span>
      )}
    </>
  );
  if (interactive) {
    return (
      <button
        type="button"
        data-testid={testId}
        title={title}
        onClick={onOpen}
        className={`${className} min-h-11 text-left transition-colors hover:border-neutral-800 hover:bg-workbench-hover/45 hover:text-neutral-300 md:min-h-8 workbench-focus`}
      >
        {content}
      </button>
    );
  }
  return (
    <div
      data-testid={testId}
      title={title}
      className={className}
    >
      {content}
    </div>
  );
}
