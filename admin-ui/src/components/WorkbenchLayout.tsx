import type { ReactNode } from 'react';

export interface WorkbenchTab<T extends string> {
  id: T;
  label: string;
  badge?: ReactNode;
  disabled?: boolean;
}

export function WorkbenchFrame({ children }: { children: ReactNode }) {
  return (
    <main className="min-h-0 min-w-0 flex-1 overflow-hidden border-y border-neutral-800 bg-[#05080c]">
      {children}
    </main>
  );
}

export function WorkbenchSurface<T extends string>({
  title,
  subtitle,
  tabs,
  activeTab,
  onTabChange,
  actions,
  children,
  flush = false,
  tabLayout = 'inline',
  contentClassName,
}: {
  title: string;
  subtitle?: ReactNode;
  tabs?: WorkbenchTab<T>[];
  activeTab?: T;
  onTabChange?: (tab: T) => void;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  tabLayout?: 'inline' | 'stacked';
  contentClassName?: string;
}) {
  const hasTabs = Boolean(tabs && tabs.length > 0);
  const tabStrip = hasTabs ? (
    <nav
      data-viewport-allow-overflow
      data-viewport-allow-truncation
      className={`flex min-w-0 items-center gap-1 overflow-x-auto ${
        tabLayout === 'stacked' ? '-mx-3 border-t border-neutral-800 px-3 pt-2 md:-mx-4 md:px-4' : 'flex-1'
      }`}
      aria-label={`${title} sections`}
    >
      {tabs!.map(tab => {
        const selected = tab.id === activeTab;
        return (
          <button
            key={tab.id}
            type="button"
            disabled={tab.disabled}
            onClick={() => onTabChange?.(tab.id)}
            aria-pressed={selected}
            className={`inline-flex min-h-[44px] shrink-0 items-center gap-1.5 rounded-md border px-2.5 text-sm font-semibold transition-colors md:min-h-0 md:py-1 ${
              selected
                ? 'border-neutral-700 bg-neutral-800 text-neutral-100 shadow-[0_1px_0_rgba(255,255,255,0.05)]'
                : 'border-transparent text-neutral-300 hover:bg-neutral-900 hover:text-neutral-100'
            } disabled:cursor-not-allowed disabled:opacity-40`}
          >
            <span>{tab.label}</span>
            {tab.badge}
          </button>
        );
      })}
    </nav>
  ) : null;
  const bodyClassName = contentClassName ?? (
    flush
      ? 'flex min-h-0 min-w-0 flex-col overflow-hidden'
      : 'min-h-0 min-w-0 overflow-auto p-3 custom-scrollbar md:p-4'
  );

  return (
    <section className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] bg-[#05080c]">
      <header className="min-w-0 border-b border-neutral-800 bg-[#05070a] px-3 py-3 md:px-4">
        <div className="flex min-w-0 items-center gap-3">
          <div className="min-w-0 shrink-0">
            <h1 className="truncate text-lg font-semibold leading-6 text-neutral-100">{title}</h1>
            {subtitle && <div className="mt-0.5 truncate text-xs text-neutral-500">{subtitle}</div>}
          </div>
          {tabLayout === 'inline' && tabStrip}
          {actions && <div className="ml-auto flex min-w-0 shrink-0 flex-wrap items-center justify-end gap-2">{actions}</div>}
        </div>
        {tabLayout === 'stacked' && <div className="mt-3">{tabStrip}</div>}
      </header>
      <div className={bodyClassName}>
        {children}
      </div>
    </section>
  );
}

export function WorkbenchSplit({
  main,
  rail,
  railClassName = '',
}: {
  main: ReactNode;
  rail?: ReactNode;
  railClassName?: string;
}) {
  return (
    <div className="grid h-full min-h-0 min-w-0 grid-cols-1 overflow-hidden lg:grid-cols-[minmax(0,1fr)_minmax(320px,28vw)]">
      <div className="flex min-h-0 min-w-0 flex-col overflow-hidden">{main}</div>
      {rail && (
        <aside className={`hidden min-h-0 min-w-0 overflow-auto border-l border-neutral-800 bg-[#10151c] p-4 custom-scrollbar lg:block ${railClassName}`}>
          {rail}
        </aside>
      )}
    </div>
  );
}

export function WorkbenchPanel({
  title,
  meta,
  children,
  className = '',
}: {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 rounded-md border border-neutral-800 bg-[#10151c] ${className}`}>
      <div className="flex min-w-0 items-center justify-between gap-3 border-b border-neutral-800 px-3 py-2.5">
        <h2 className="truncate text-sm font-semibold text-neutral-100">{title}</h2>
        {meta && <div className="shrink-0 text-xs text-neutral-500">{meta}</div>}
      </div>
      <div className="min-w-0 p-3">{children}</div>
    </section>
  );
}

export function WorkbenchMetric({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info';
}) {
  const toneClass = {
    neutral: 'text-neutral-100',
    good: 'text-emerald-300',
    warn: 'text-amber-300',
    bad: 'text-red-300',
    info: 'text-cyan-300',
  }[tone];
  return (
    <div className="rounded-md border border-neutral-800 bg-[#151c25] px-3 py-2">
      <div className="text-ui-md uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

export function WorkbenchStatusBar({
  left,
  right,
}: {
  left: ReactNode;
  right?: ReactNode;
}) {
  return (
    <footer className="hidden h-6 shrink-0 items-center justify-between gap-3 border-t border-neutral-800 bg-[#05070a] px-2 font-mono text-ui-md text-neutral-500 md:flex">
      <div className="flex min-w-0 items-center gap-3 truncate">{left}</div>
      {right && <div className="flex shrink-0 items-center gap-3">{right}</div>}
    </footer>
  );
}
