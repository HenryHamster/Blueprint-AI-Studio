import { useState, useEffect, useCallback } from 'react';

const VALID_DAYS = [7, 14, 30, 90] as const;

function parseDays(val: string | null, fallback: number): number {
  if (!val) return fallback;
  const n = parseInt(val, 10);
  return (VALID_DAYS as readonly number[]).includes(n) ? n : fallback;
}

function parsePage(val: string | null): number {
  if (!val) return 1;
  const n = parseInt(val, 10);
  return Number.isInteger(n) && n >= 1 ? n : 1;
}

type FilterInternal = {
  projectId: string | null;
  days: number;
  secondaryFilter: string;
  taskTypeFilter: string;
  scopeFilter: string;
  severityFilter: string;
  page: number;
};

export interface UrlFilterState {
  days: number;
  secondaryFilter: string;
  taskTypeFilter: string;
  scopeFilter: string;
  severityFilter: string;
  page: number;
  setDays: (d: number) => void;
  setSecondaryFilter: (c: string) => void;
  setTaskTypeFilter: (t: string) => void;
  setScopeFilter: (s: string) => void;
  setSeverityFilter: (s: string) => void;
  setPage: (p: number | ((prev: number) => number)) => void;
}

export interface UrlFilterStateOptions {
  defaultDays?: number;
  /** URL param name for the secondary filter (default 'category'). */
  secondaryParam?: string;
  /** When true, the hook also tracks a 'scope' filter under `<prefix>scope`. */
  withScope?: boolean;
  /** When true, the hook also tracks a 'min_severity' filter under `<prefix>min_severity`. */
  withSeverity?: boolean;
}

/**
 * Persists days/secondary/task_type/(scope)/page filter state in URL query
 * params using the given prefix. The "secondary" field carries panel-specific
 * semantics (category for the missed-retros panel, blind_spot for the
 * unhandled-edges panel) — its URL param name is configurable via
 * `options.secondaryParam`.
 */
export function useUrlFilterState(
  prefix: string,
  projectId: string | null,
  options: UrlFilterStateOptions = {},
): UrlFilterState {
  const { defaultDays = 14, secondaryParam = 'category', withScope = false, withSeverity = false } = options;

  const [state, setState] = useState<FilterInternal>(() => {
    const params = new URLSearchParams(window.location.search);
    return {
      projectId,
      days: parseDays(params.get(`${prefix}days`), defaultDays),
      secondaryFilter: params.get(`${prefix}${secondaryParam}`) ?? '',
      taskTypeFilter: params.get(`${prefix}task_type`) ?? '',
      scopeFilter: withScope ? (params.get(`${prefix}scope`) ?? '') : '',
      severityFilter: withSeverity ? (params.get(`${prefix}min_severity`) ?? '') : '',
      page: parsePage(params.get(`${prefix}page`)),
    };
  });

  // Derived state reset: when the selected project changes, reset filters synchronously during render.
  // Calling setState during render causes React to immediately discard this render and re-render
  // with the new state, following the React "key-less derived state" pattern.
  if (state.projectId !== projectId) {
    setState({
      projectId,
      days: defaultDays,
      secondaryFilter: '',
      taskTypeFilter: '',
      scopeFilter: '',
      severityFilter: '',
      page: 1,
    });
  }

  // Sync state → URL (preserves params from other prefixes)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);

    if (state.days !== defaultDays) {
      params.set(`${prefix}days`, String(state.days));
    } else {
      params.delete(`${prefix}days`);
    }

    if (state.secondaryFilter) {
      params.set(`${prefix}${secondaryParam}`, state.secondaryFilter);
    } else {
      params.delete(`${prefix}${secondaryParam}`);
    }

    if (state.taskTypeFilter) {
      params.set(`${prefix}task_type`, state.taskTypeFilter);
    } else {
      params.delete(`${prefix}task_type`);
    }

    if (withScope) {
      if (state.scopeFilter) {
        params.set(`${prefix}scope`, state.scopeFilter);
      } else {
        params.delete(`${prefix}scope`);
      }
    }

    if (withSeverity) {
      if (state.severityFilter) {
        params.set(`${prefix}min_severity`, state.severityFilter);
      } else {
        params.delete(`${prefix}min_severity`);
      }
    }

    if (state.page !== 1) {
      params.set(`${prefix}page`, String(state.page));
    } else {
      params.delete(`${prefix}page`);
    }

    const search = params.toString();
    window.history.replaceState(
      null,
      '',
      search ? `${window.location.pathname}?${search}` : window.location.pathname,
    );
  }, [prefix, secondaryParam, withScope, withSeverity, state, defaultDays]);

  // Stable setter references (setState from useState is always stable)
  const setDays = useCallback((d: number) => setState(s => ({ ...s, days: d })), []);
  const setSecondaryFilter = useCallback((c: string) => setState(s => ({ ...s, secondaryFilter: c })), []);
  const setTaskTypeFilter = useCallback((t: string) => setState(s => ({ ...s, taskTypeFilter: t })), []);
  const setScopeFilter = useCallback((s: string) => setState(prev => ({ ...prev, scopeFilter: s })), []);
  const setSeverityFilter = useCallback((s: string) => setState(prev => ({ ...prev, severityFilter: s })), []);
  const setPage = useCallback(
    (p: number | ((prev: number) => number)) =>
      setState(s => ({ ...s, page: typeof p === 'function' ? p(s.page) : p })),
    [],
  );

  return {
    days: state.days,
    secondaryFilter: state.secondaryFilter,
    taskTypeFilter: state.taskTypeFilter,
    scopeFilter: state.scopeFilter,
    severityFilter: state.severityFilter,
    page: state.page,
    setDays,
    setSecondaryFilter,
    setTaskTypeFilter,
    setScopeFilter,
    setSeverityFilter,
    setPage,
  };
}
