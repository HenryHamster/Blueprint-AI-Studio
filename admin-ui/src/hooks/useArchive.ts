import { useState, useRef } from 'react';
import type { Task } from '../types/task';
import { API_BASE_URL, authGet } from '../api';
import { usePersistedState } from './usePersistedState';

export function useArchive(addToast: (msg: string, type: 'success' | 'error') => void) {
  const [showArchive, setShowArchiveState] = useState(false);
  const [archiveTasks, setArchiveTasks] = useState<Task[]>([]);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archivePage, setArchivePageState] = useState(1);
  const [archivePageSize, setArchivePageSizeState] = usePersistedState<number>('archivePageSize', 50);
  const [archiveStatusFilter, setArchiveStatusFilterState] = useState<string>('all');
  const [archiveSearchQuery, setArchiveSearchQueryState] = useState('');
  const [archiveDateRange, setArchiveDateRangeState] = useState<string>('all');
  const [archiveTotalTasks, setArchiveTotalTasks] = useState(0);
  const [archiveSortOrder, setArchiveSortOrderState] = useState<'desc' | 'asc'>('desc');

  const archiveSearchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Use refs to always read latest filter state inside fetchArchiveTasks without needing
  // to pass overrides through every call-site.  The override parameters are kept for
  // backwards-compat with callers that set state and fetch in the same tick.
  const archivePageRef = useRef(archivePage);
  archivePageRef.current = archivePage;
  const archivePageSizeRef = useRef(archivePageSize);
  archivePageSizeRef.current = archivePageSize;
  const archiveStatusFilterRef = useRef(archiveStatusFilter);
  archiveStatusFilterRef.current = archiveStatusFilter;
  const archiveSearchQueryRef = useRef(archiveSearchQuery);
  archiveSearchQueryRef.current = archiveSearchQuery;
  const archiveDateRangeRef = useRef(archiveDateRange);
  archiveDateRangeRef.current = archiveDateRange;
  const archiveSortOrderRef = useRef(archiveSortOrder);
  archiveSortOrderRef.current = archiveSortOrder;

  const fetchArchiveTasks = async (
    pageOverride?: number,
    pageSizeOverride?: number,
    statusOverride?: string,
    searchOverride?: string,
    dateRangeOverride?: string,
    sortOrderOverride?: 'desc' | 'asc',
  ) => {
    setArchiveLoading(true);
    try {
      const p = pageOverride ?? archivePageRef.current;
      const ps = pageSizeOverride ?? archivePageSizeRef.current;
      const status = statusOverride ?? archiveStatusFilterRef.current;
      const search = searchOverride ?? archiveSearchQueryRef.current;
      const dateRange = dateRangeOverride ?? archiveDateRangeRef.current;
      const sortOrder = sortOrderOverride ?? archiveSortOrderRef.current;
      const offset = (p - 1) * ps;
      let url = `${API_BASE_URL}/api/admin/tasks?limit=${ps}&offset=${offset}&archived=true`;
      if (status !== 'all') {
        url += `&status=${status}`;
      }
      if (search.trim()) {
        url += `&search=${encodeURIComponent(search.trim())}`;
      }
      if (dateRange !== 'all') {
        const now = new Date();
        const hours = dateRange === '24h' ? 24 : dateRange === '7d' ? 24 * 7 : dateRange === '30d' ? 24 * 30 : 0;
        if (hours > 0) {
          const after = new Date(now.getTime() - hours * 60 * 60 * 1000);
          url += `&created_after=${encodeURIComponent(after.toISOString())}`;
        }
      }
      url += `&sort_order=${sortOrder}`;
      const res = await authGet(url);
      if (res.ok) {
        const data = await res.json();
        let tasks: Task[] = data.tasks || [];
        // Frontend fallback sort in case the backend ignores the sort_order param
        tasks = [...tasks].sort((a, b) => {
          const diff = new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
          return sortOrder === 'asc' ? -diff : diff;
        });
        setArchiveTasks(tasks);
        setArchiveTotalTasks(data.total || 0);
      }
    } catch (err) {
      console.error('Failed to fetch archive tasks:', err);
      addToast('Failed to load archive', 'error');
    } finally {
      setArchiveLoading(false);
    }
  };

  const setShowArchive = (show: boolean) => {
    setShowArchiveState(show);
    if (show) {
      fetchArchiveTasks();
    }
  };

  const setArchivePage = (page: number) => {
    setArchivePageState(page);
    fetchArchiveTasks(page);
  };

  const setArchivePageSize = (size: number) => {
    setArchivePageSizeState(size);
    setArchivePageState(1);
    fetchArchiveTasks(1, size);
  };

  const setArchiveStatusFilter = (status: string) => {
    setArchiveStatusFilterState(status);
    setArchivePageState(1);
    fetchArchiveTasks(1, undefined, status);
  };

  const setArchiveSearchQuery = (query: string) => {
    setArchiveSearchQueryState(query);
    if (archiveSearchDebounceRef.current) {
      clearTimeout(archiveSearchDebounceRef.current);
    }
    archiveSearchDebounceRef.current = setTimeout(() => {
      setArchivePageState(1);
      fetchArchiveTasks(1, undefined, undefined, query);
    }, 300);
  };

  const setArchiveDateRange = (range: string) => {
    setArchiveDateRangeState(range);
    setArchivePageState(1);
    fetchArchiveTasks(1, undefined, undefined, undefined, range);
  };

  const setArchiveSortOrder = (order: 'desc' | 'asc') => {
    setArchiveSortOrderState(order);
    setArchivePageState(1);
    fetchArchiveTasks(1, undefined, undefined, undefined, undefined, order);
  };

  return {
    showArchive, setShowArchive, archiveTasks, archiveLoading,
    archivePage, setArchivePage, archivePageSize, setArchivePageSize,
    archiveStatusFilter, setArchiveStatusFilter,
    archiveSearchQuery, setArchiveSearchQuery,
    archiveDateRange, setArchiveDateRange,
    archiveTotalTasks, fetchArchiveTasks,
    archiveSortOrder, setArchiveSortOrder,
  };
}
