import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

export interface WorkbenchTab<T extends string> {
  id: T;
  label: string;
  badge?: ReactNode;
  disabled?: boolean;
}

export function WorkbenchFrame({ children }: { children: ReactNode }) {
  return (
    <main className="min-h-0 min-w-0 flex-1 overflow-hidden border-b border-workbench-border bg-workbench-base">
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
  showTitle = true,
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
  showTitle?: boolean;
  tabLayout?: 'inline' | 'stacked';
  contentClassName?: string;
}) {
  const hasTabs = Boolean(tabs && tabs.length > 0);
  const activeTabRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    activeTabRef.current?.scrollIntoView({
      block: 'nearest',
      inline: 'center',
    });
  }, [activeTab]);

  const tabStrip = hasTabs ? (
    <nav
      data-viewport-allow-overflow
      data-viewport-allow-truncation
      className={`flex min-w-0 items-center gap-0.5 overflow-x-auto ${
        tabLayout === 'stacked' ? '-mx-2 border-t border-neutral-800/90 px-2 pt-1.5 md:-mx-2.5 md:px-2.5' : 'flex-1'
      }`}
      aria-label={`${title} sections`}
    >
      {tabs!.map(tab => {
        const selected = tab.id === activeTab;
        return (
          <button
            key={tab.id}
            ref={selected ? activeTabRef : undefined}
            type="button"
            disabled={tab.disabled}
            onClick={() => onTabChange?.(tab.id)}
            aria-pressed={selected}
            className={`workbench-focus inline-flex min-h-[44px] shrink-0 items-center gap-1.5 rounded border px-2.5 text-sm font-semibold transition-colors md:min-h-6 md:px-2 md:py-0.5 md:text-xs ${
              selected
                ? 'border-workbench-border-strong bg-neutral-800/90 text-neutral-100 ring-1 ring-inset ring-white/5'
                : 'border-transparent text-neutral-400 hover:bg-workbench-layer hover:text-neutral-100'
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
      : 'min-h-0 min-w-0 overflow-auto p-2 custom-scrollbar'
  );

  const headerClassName = showTitle
    ? 'min-w-0 border-b border-workbench-border/90 bg-workbench-shell px-2 py-1 md:px-2'
    : 'min-w-0 border-b border-workbench-border/90 bg-workbench-shell px-2 py-0.5 md:px-2';

  return (
    <section className="grid h-full min-h-0 min-w-0 overflow-hidden grid-rows-[auto_minmax(0,1fr)] bg-workbench-surface">
      <header className={headerClassName}>
        <div className="flex min-w-0 items-center gap-2">
          {showTitle && (
            <div className="min-w-0 shrink-0">
              <h1 className="truncate text-sm font-semibold leading-5 text-neutral-100">{title}</h1>
              {subtitle && <div className="mt-0.5 truncate text-xs text-neutral-500">{subtitle}</div>}
            </div>
          )}
          {tabLayout === 'inline' && tabStrip}
          {actions && <div className={`${showTitle || hasTabs ? 'ml-auto' : 'mx-auto w-full max-w-[940px] lg:mx-0'} flex min-w-0 shrink-0 flex-wrap items-center justify-end gap-1`}>{actions}</div>}
        </div>
        {tabLayout === 'stacked' && <div className="mt-1.5">{tabStrip}</div>}
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
  railCollapsed = false,
}: {
  main: ReactNode;
  rail?: ReactNode;
  railClassName?: string;
  /** When true, the rail is hidden and the main column spans full width. */
  railCollapsed?: boolean;
}) {
  const showRail = Boolean(rail) && !railCollapsed;
  return (
    <div
      className={`grid h-full min-h-0 min-w-0 grid-cols-1 overflow-hidden ${
        showRail ? 'lg:grid-cols-[minmax(0,1fr)_minmax(300px,26vw)]' : ''
      }`}
    >
      <div className="flex min-h-0 min-w-0 flex-col overflow-hidden">{main}</div>
      {showRail && (
        <aside className={`hidden min-h-0 min-w-0 overflow-auto border-l border-workbench-border bg-workbench-rail p-1.5 custom-scrollbar lg:block ${railClassName}`}>
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
    <section className={`workbench-panel min-w-0 rounded-md border ${className}`}>
      <div className="flex min-w-0 items-center justify-between gap-3 border-b border-workbench-border px-2 py-1.5">
        <h2 className="truncate text-sm font-semibold text-neutral-100">{title}</h2>
        {meta && <div className="shrink-0 text-xs text-neutral-500">{meta}</div>}
      </div>
      <div className="min-w-0 p-1.5">{children}</div>
    </section>
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
    <footer className="hidden h-5 shrink-0 items-center justify-between gap-3 border-t border-workbench-border bg-workbench-control px-2 font-mono text-ui-md text-neutral-500 md:flex">
      <div className="flex min-w-0 items-center gap-3 truncate">{left}</div>
      {right && <div className="flex shrink-0 items-center gap-3">{right}</div>}
    </footer>
  );
}
