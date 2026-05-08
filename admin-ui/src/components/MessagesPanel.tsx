import { useState, useEffect, useCallback, useRef } from 'react';
import { X, MessageSquare, Send, Check, AlertTriangle, HelpCircle, Bug, StickyNote, ChevronDown, PlusCircle, Trash2, CheckSquare, ArrowDown, Reply, Settings } from 'lucide-react';
import { auth } from '../firebase';
import { apiFetch, API_BASE_URL } from '../api';
import { parseTimestamp } from '../utils/time';
import { routineSummary, effectiveSignal, loadEventPrefs, EVENT_PREFS_KEY, type EventPref, groupRoutineMessages, outcomeBreakdown, isFailingGroup, getToastSeverity, type RoutineRow } from './MessagesPanel.utils';

interface ProjectMessage {
  id: string;
  project_id: string;
  message_type: string;
  from_type: string;
  from_task_id: string | null;
  content: string;
  context: Record<string, unknown>;
  status: string;
  resolution: string | null;
  resolved_at: string | null;
  created_at: string | null;
}

interface MessagesPanelProps {
  selectedProjectId: string | null;
  onClose: () => void;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onMessageCountChange?: (count: number) => void;
}

type MessagesPanelPrefs = { version: 1; routineExpanded: boolean; showResolved: boolean };

function messagesPanelPrefsKey(userId?: string | null): string {
  return `messages_panel_prefs:${userId || 'anonymous'}`;
}

function loadMessagesPanelPrefs(userId?: string | null): MessagesPanelPrefs {
  try {
    const raw = localStorage.getItem(messagesPanelPrefsKey(userId));
    if (!raw) return { version: 1, routineExpanded: false, showResolved: false };
    const parsed = JSON.parse(raw) as Partial<MessagesPanelPrefs>;
    if (parsed.version !== 1 || typeof parsed.routineExpanded !== 'boolean' || typeof parsed.showResolved !== 'boolean') {
      return { version: 1, routineExpanded: false, showResolved: false };
    }
    return parsed as MessagesPanelPrefs;
  } catch {
    return { version: 1, routineExpanded: false, showResolved: false };
  }
}

function saveMessagesPanelPrefs(userId: string | null | undefined, prefs: MessagesPanelPrefs): void {
  try { localStorage.setItem(messagesPanelPrefsKey(userId), JSON.stringify(prefs)); } catch { /* ignore */ }
}

const TYPE_CONFIG: Record<string, { icon: typeof MessageSquare; color: string; label: string; bubbleBg: string }> = {
  QUESTION:   { icon: HelpCircle,    color: 'text-blue-400',    label: 'Question',   bubbleBg: 'bg-blue-950/40 border-blue-800/40' },
  FEEDBACK:   { icon: MessageSquare,  color: 'text-purple-400',  label: 'Feedback',   bubbleBg: 'bg-purple-950/40 border-purple-800/40' },
  BUG_REPORT: { icon: Bug,           color: 'text-red-400',     label: 'Bug',        bubbleBg: 'bg-red-950/50 border-red-800/50' },
  BLOCKER:    { icon: AlertTriangle,  color: 'text-amber-400',   label: 'Blocker',    bubbleBg: 'bg-amber-950/50 border-amber-700/50' },
  NOTE:       { icon: StickyNote,     color: 'text-neutral-400',  label: 'Note',       bubbleBg: 'bg-neutral-900/60 border-neutral-800/50' },
};

const MSG_TYPE_OPTIONS = [
  { label: 'Feedback', value: 'FEEDBACK' },
  { label: 'Question', value: 'QUESTION' },
  { label: 'Bug Report', value: 'BUG_REPORT' },
  { label: 'Blocker', value: 'BLOCKER' },
  { label: 'Note', value: 'NOTE' },
];

function MessageTypeDropdown({ value, onChange }: { value: string; onChange: (v: string) => void }) {
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

function formatAge(isoString: string | null): string {
  if (!isoString) return '';
  const ms = Date.now() - parseTimestamp(isoString).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function formatFromType(msg: ProjectMessage): string {
  const ft = (msg.from_type || '').toLowerCase();
  if (ft === 'agent') {
    const taskSlice = msg.from_task_id ? ` (${msg.from_task_id.slice(0, 8)})` : '';
    return `Agent${taskSlice}`;
  }
  if (ft === 'strategic') return 'Strategic Review';
  if (ft === 'system') return 'System';
  return 'You';
}

function isUserMessage(msg: ProjectMessage): boolean {
  return (msg.from_type || '').toLowerCase() === 'user';
}

function ContextBadges({ context, fromType }: { context: Record<string, unknown>; fromType: string }) {
  if ((fromType || '').toLowerCase() === 'user') return null;
  const badges: { label: string; className: string }[] = [];

  const severity = context.severity as string | undefined;
  if (severity === 'high') badges.push({ label: 'High', className: 'text-red-400 bg-red-500/10 border-red-500/30' });
  else if (severity === 'medium') badges.push({ label: 'Medium', className: 'text-amber-400 bg-amber-500/10 border-amber-500/30' });
  else if (severity === 'low') badges.push({ label: 'Low', className: 'text-neutral-500 bg-neutral-500/10 border-neutral-600/30' });

  const annotationType = context.annotation_type as string | undefined;
  if (annotationType === 'vision') badges.push({ label: 'Vision', className: 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30' });
  else if (annotationType === 'issue') badges.push({ label: 'Issue', className: 'text-red-400 bg-red-500/10 border-red-500/30' });
  else if (annotationType === 'comment') badges.push({ label: 'Comment', className: 'text-neutral-400 bg-neutral-500/10 border-neutral-600/30' });

  const taskType = context.task_type as string | undefined;
  if (taskType) badges.push({ label: taskType, className: 'text-neutral-500 bg-neutral-500/10 border-neutral-600/30' });

  const source = context.source as string | undefined;
  if (source === 'research_zero_children') badges.push({ label: 'No findings', className: 'text-amber-400 bg-amber-500/10 border-amber-500/30' });

  const eventType = context.event_type as string | undefined;
  if (eventType === 'pr_merged') badges.push({ label: 'PR Merged', className: 'text-green-400 bg-green-500/10 border-green-500/30' });
  else if (eventType === 'direction_milestone') badges.push({ label: (context.milestone as string) || 'Milestone', className: 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30' });
  else if (eventType === 'cycle_summary') badges.push({ label: 'Cycle Summary', className: 'text-blue-400 bg-blue-500/10 border-blue-500/30' });
  else if (eventType === 'failure_alert') badges.push({ label: 'Failure Alert', className: 'text-red-400 bg-red-500/10 border-red-500/30' });
  else if (eventType === 'feedback_reply') badges.push({ label: 'Feedback Reply', className: 'text-purple-400 bg-purple-500/10 border-purple-500/30' });

  if (badges.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1 mt-1.5">
      {badges.map((b, i) => (
        <span key={i} className={`text-[11px] px-1.5 py-0.5 rounded border ${b.className}`}>{b.label}</span>
      ))}
    </div>
  );
}


export function MessagesPanel({ selectedProjectId, onClose, addToast, onMessageCountChange }: MessagesPanelProps) {
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const [loading, setLoading] = useState(true);
  // Compose
  const [newType, setNewType] = useState('FEEDBACK');
  const [newContent, setNewContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [replyTo, setReplyTo] = useState<{ id: string; content: string; from: string } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Per-message action state
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const [resolveInput, setResolveInput] = useState('');
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const [creatingDirectionIds, setCreatingDirectionIds] = useState<Set<string>>(new Set());

  // Batch selection
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchCreating, setBatchCreating] = useState(false);

  // Scroll state
  const scrollRef = useRef<HTMLDivElement>(null);
  const [showScrollDown, setShowScrollDown] = useState(false);
  const prevMessageCountRef = useRef(0);

  // Collapsed resolved
  const [showResolved, setShowResolved] = useState(() => loadMessagesPanelPrefs(auth.currentUser?.email).showResolved);
  // Routine ops group collapsed by default
  const [routineExpanded, setRoutineExpanded] = useState(() => loadMessagesPanelPrefs(auth.currentUser?.email).routineExpanded);
  // Track which grouped-chip event_type keys are expanded
  const [expandedGroupKeys, setExpandedGroupKeys] = useState<Set<string>>(new Set());
  // Per-event-type mute/surface preferences
  const [eventPrefs, setEventPrefs] = useState<Record<string, EventPref>>(loadEventPrefs);
  const [showPrefsPanel, setShowPrefsPanel] = useState(false);
  const [sortMode, setSortMode] = useState<'importance' | 'recent'>('importance');

  // Stable ref for addToast
  const addToastRef = useRef(addToast);
  addToastRef.current = addToast;

  // Track seen messages to detect new arrivals and avoid initial-load toast spam
  const seenMessageIdsRef = useRef<Set<string>>(new Set());
  const hasInitiallyLoadedRef = useRef(false);
  const seenPromotedGroupKeysRef = useRef<Set<string>>(new Set());
  const eventPrefsRef = useRef(eventPrefs);
  eventPrefsRef.current = eventPrefs;

  // Reset toast suppression state when switching projects so that the next
  // fetch is treated as an initial load for the new project (avoids a flood
  // of toasts for project B's pre-existing open BLOCKER/BUG/FEEDBACK items).
  useEffect(() => {
    seenMessageIdsRef.current = new Set();
    seenPromotedGroupKeysRef.current = new Set();
    hasInitiallyLoadedRef.current = false;
  }, [selectedProjectId]);

  const byTime = (a: ProjectMessage, b: ProjectMessage) => {
    const ta = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
    const tb = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
    return ta - tb;
  };

  const byTimeDesc = (a: ProjectMessage, b: ProjectMessage) => -byTime(a, b);

  const SEVERITY_RANK: Record<string, number> = { BLOCKER: 0, BUG_REPORT: 1, FEEDBACK: 2, NOTE: 3 };
  const bySeverity = (a: ProjectMessage, b: ProjectMessage) => {
    const ra = SEVERITY_RANK[a.message_type] ?? 3;
    const rb = SEVERITY_RANK[b.message_type] ?? 3;
    if (ra !== rb) return ra - rb;
    return byTimeDesc(a, b);
  };

  const openMessages = messages.filter(m => m.status === 'open');
  const resolvedMessages = messages.filter(m => m.status === 'resolved');
  const openFeedbackMessages = openMessages.filter(m => m.message_type === 'FEEDBACK');

  // Split open messages by signal level, respecting per-event-type prefs
  const highSignalOpen = [...openMessages.filter(m => effectiveSignal(m, eventPrefs) === 'high')]
    .sort(sortMode === 'importance' ? bySeverity : byTimeDesc);
  const routineOpen = [...openMessages.filter(m => effectiveSignal(m, eventPrefs) === 'routine')]
    .sort(byTimeDesc);

  // In importance mode, pin BLOCKERs at the top with a visual divider
  const pinnedBlockers = sortMode === 'importance' ? highSignalOpen.filter(m => m.message_type === 'BLOCKER') : [];
  const otherHighSignal = sortMode === 'importance' ? highSignalOpen.filter(m => m.message_type !== 'BLOCKER') : highSignalOpen;

  // Failing routine groups auto-promoted above the routine ops section
  const allRoutineRows = groupRoutineMessages(routineOpen);
  const promotedGroups = allRoutineRows.filter(
    (r): r is RoutineRow<ProjectMessage> & { kind: 'group' } =>
      r.kind === 'group' && isFailingGroup(r.msgs) && eventPrefs[r.event_type] !== 'mute',
  );
  const promotedKeys = new Set(promotedGroups.map(r => r.key));
  const remainingRoutineRows = allRoutineRows.filter(r => r.kind === 'single' || !promotedKeys.has(r.key));

  const allEventTypes = [...new Set([
    ...Object.keys(eventPrefs),
    ...openMessages.flatMap(m => { const et = m.context.event_type as string | undefined; return et ? [et] : []; }),
  ])];
  const mutedCount = Object.values(eventPrefs).filter(v => v === 'mute').length;

  function updatePref(et: string, pref: EventPref) {
    setEventPrefs(prev => {
      const next = pref === 'routine'
        ? Object.fromEntries(Object.entries(prev).filter(([k]) => k !== et)) as Record<string, EventPref>
        : { ...prev, [et]: pref };
      localStorage.setItem(EVENT_PREFS_KEY, JSON.stringify(next));
      return next;
    });
  }

  useEffect(() => {
    saveMessagesPanelPrefs(auth.currentUser?.email, { version: 1, routineExpanded, showResolved });
  }, [routineExpanded, showResolved]);

  // Resolved: show last 5 by default, toggle to show all
  const RECENT_RESOLVED = 5;
  const sortedResolved = [...resolvedMessages].sort(byTime);
  const visibleResolved = showResolved ? sortedResolved : sortedResolved.slice(-RECENT_RESOLVED);
  const hiddenResolvedCount = showResolved ? 0 : Math.max(0, resolvedMessages.length - RECENT_RESOLVED);

  const fetchMessages = useCallback(async () => {
    if (!selectedProjectId) { setLoading(false); return; }
    try {
      const params = new URLSearchParams({ project_id: selectedProjectId, status: 'all', limit: '100' });
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const msgs: ProjectMessage[] = data.messages ?? [];

      // Detect new high-signal messages for toast alerts
      if (hasInitiallyLoadedRef.current) {
        const newHighSignal = msgs.filter(m =>
          m.status === 'open' &&
          effectiveSignal(m, eventPrefs) === 'high' &&
          !seenMessageIdsRef.current.has(m.id)
        );

        if (newHighSignal.length > 0) {
          const summary = newHighSignal.length === 1
            ? `New ${newHighSignal[0].message_type === 'BLOCKER' ? 'BLOCKER' : newHighSignal[0].message_type === 'BUG_REPORT' ? 'BUG' : newHighSignal[0].message_type === 'FEEDBACK' ? 'FEEDBACK' : 'high-signal message'}: ${newHighSignal[0].content.slice(0, 80)}`
            : `${newHighSignal.length} new high-signal messages`;

          // Use the highest severity among the new messages
          const toastSeverity = newHighSignal.some(m => getToastSeverity(m) === 'error') ? 'error' : 'info';
          addToastRef.current(summary, toastSeverity);
        }
      }

      // Detect + toast for newly promoted failing routine groups
      const wasLoaded = hasInitiallyLoadedRef.current;
      const routineMsgs = msgs.filter(m => m.status === 'open' && effectiveSignal(m, eventPrefsRef.current) === 'routine');
      for (const row of groupRoutineMessages(routineMsgs)) {
        if (row.kind === 'group' && isFailingGroup(row.msgs) && eventPrefsRef.current[row.event_type] !== 'mute') {
          if (wasLoaded && !seenPromotedGroupKeysRef.current.has(row.key)) {
            const bd = outcomeBreakdown(row.msgs);
            addToastRef.current(`Failing group: ${row.msgs.length} ${row.label} ${bd}`, 'error');
          }
          seenPromotedGroupKeysRef.current.add(row.key);
        }
      }

      // Update tracking of seen message IDs
      msgs.forEach(m => seenMessageIdsRef.current.add(m.id));
      hasInitiallyLoadedRef.current = true;

      // Only update state if messages actually changed — prevents scroll jumps from polling
      setMessages(prev => {
        if (prev.length === msgs.length && prev.every((m, i) => m.id === msgs[i].id && m.status === msgs[i].status)) return prev;
        return msgs;
      });
      const openCount = msgs.filter(m => m.status === 'open' && (m.from_type || '').toLowerCase() !== 'user').length;
      onMessageCountChange?.(openCount);
    } catch {
      addToastRef.current('Failed to load messages', 'error');
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, onMessageCountChange, eventPrefs]);

  useEffect(() => {
    fetchMessages();
    const interval = setInterval(fetchMessages, 30_000);
    return () => clearInterval(interval);
  }, [fetchMessages]);

  // Auto-scroll to bottom only on initial load or when user sends a new message
  // (new message = count increased AND user is near the bottom)
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const currentCount = highSignalOpen.length + routineOpen.length + visibleResolved.length;
    const isInitialLoad = prevMessageCountRef.current === 0 && currentCount > 0;
    if (isInitialLoad) {
      // First load — jump to bottom instantly
      requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'instant' }));
    } else if (currentCount > prevMessageCountRef.current) {
      // New message arrived — only scroll if user is already near bottom
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      if (nearBottom) {
        requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }));
      }
    }
    // Don't scroll at all on polling refresh with same count — preserves position
    prevMessageCountRef.current = currentCount;
  }, [highSignalOpen, routineOpen, visibleResolved]);

  // Track scroll position for "scroll to bottom" button
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    function onScroll() {
      if (!el) return;
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      setShowScrollDown(gap > 150);
    }
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  function scrollToBottom() {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }

  // Auto-resize textarea
  function handleTextareaInput() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`; // max 3 rows ~96px
  }

  async function handleSubmit() {
    if (!selectedProjectId || !newContent.trim()) return;
    setSubmitting(true);
    try {
      const context: Record<string, unknown> = {};
      if (replyTo) {
        context.reply_to = replyTo.id;
        context.reply_preview = replyTo.content.slice(0, 120);
      }
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: selectedProjectId,
          message_type: newType,
          content: newContent.trim(),
          ...(Object.keys(context).length > 0 ? { context } : {}),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setNewContent('');
      setReplyTo(null);
      if (textareaRef.current) textareaRef.current.style.height = 'auto';
      addToast('Message sent', 'success');
      await fetchMessages();
    } catch {
      addToast('Failed to send message', 'error');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResolve(msgId: string, resolution: string) {
    setResolvingId(null);
    setResolveInput('');
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msgId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchMessages();
    } catch {
      addToast('Failed to resolve message', 'error');
    }
  }

  async function handleDelete(msgId: string) {
    setDeletingIds(prev => new Set(prev).add(msgId));
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msgId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchMessages();
    } catch {
      addToast('Failed to delete message', 'error');
    } finally {
      setDeletingIds(prev => { const next = new Set(prev); next.delete(msgId); return next; });
    }
  }

  async function handleCreateDirection(msg: ProjectMessage, silent = false): Promise<boolean> {
    if (!selectedProjectId) return false;
    setCreatingDirectionIds(prev => new Set(prev).add(msg.id));
    try {
      const text = msg.content.slice(0, 2000);
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/directions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, priority: 'medium' }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const directionId: string = data.direction?.id ?? '';
      await apiFetch(`${API_BASE_URL}/api/admin/messages/${msg.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution: `Direction created: ${directionId}` }),
      });
      if (!silent) addToast('Direction created', 'success');
      await fetchMessages();
      return true;
    } catch {
      addToast('Failed to create direction', 'error');
      return false;
    } finally {
      setCreatingDirectionIds(prev => { const next = new Set(prev); next.delete(msg.id); return next; });
    }
  }

  const KEEP_RESOLVED = 20;

  async function handleBulkDeleteResolved() {
    // Keep the 20 most recent resolved, delete the rest
    const sorted = [...resolvedMessages].sort((a, b) => {
      const ta = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
      const tb = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
      return ta - tb;
    });
    const toDelete = sorted.slice(0, -KEEP_RESOLVED);
    if (toDelete.length === 0) {
      addToast(`Only ${sorted.length} resolved — nothing to clear`, 'info');
      return;
    }
    let count = 0;
    for (const msg of toDelete) {
      try {
        const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msg.id}`, { method: 'DELETE' });
        if (res.ok) count++;
      } catch { /* skip */ }
    }
    if (count > 0) addToast(`Cleared ${count} old resolved (kept ${Math.min(sorted.length, KEEP_RESOLVED)} recent)`, 'success');
    await fetchMessages();
  }

  function toggleSelect(msgId: string) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(msgId)) next.delete(msgId); else next.add(msgId);
      return next;
    });
  }

  async function handleBatchCreateDirections() {
    const msgs = openFeedbackMessages.filter(m => selectedIds.has(m.id));
    if (msgs.length === 0) return;
    setBatchCreating(true);
    let count = 0;
    for (const msg of msgs) {
      const ok = await handleCreateDirection(msg, true);
      if (ok) count++;
    }
    setBatchCreating(false);
    setSelectedIds(new Set());
    setSelectMode(false);
    if (count > 0) addToast(`${count} direction${count > 1 ? 's' : ''} created`, 'success');
  }

  async function handleResolveGroup(msgs: ProjectMessage[]) {
    try {
      for (const m of msgs) {
        await apiFetch(`${API_BASE_URL}/api/admin/messages/${m.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resolution: '' }),
        });
      }
      await fetchMessages();
    } catch {
      addToast('Failed to resolve messages', 'error');
    }
  }

  function renderBubble(msg: ProjectMessage, faded = false, readOnly = false, allMessages: ProjectMessage[] = []) {
    const config = TYPE_CONFIG[msg.message_type] || TYPE_CONFIG.NOTE;
    const Icon = config.icon;
    const userMsg = isUserMessage(msg);
    const isResolving = resolvingId === msg.id;

    // Look up original FEEDBACK for feedback_acknowledgment NOTEs
    let replyContextMsg: ProjectMessage | null = null;
    if (msg.message_type === 'NOTE' &&
        msg.context.event_type === 'feedback_acknowledgment' &&
        typeof msg.context.reply_to === 'string') {
      replyContextMsg = allMessages.find(m => m.id === msg.context.reply_to) || null;
    }

    return (
      <div key={msg.id} className={`flex ${userMsg ? 'justify-end' : 'justify-start'} ${faded ? 'opacity-75' : ''}`}>
        <div className={`max-w-[85%] md:max-w-[70%] ${userMsg ? 'items-end' : 'items-start'} flex flex-col`}>
          {/* Bubble */}
          <div className={`rounded-2xl border px-4 py-3 ${config.bubbleBg} ${
            msg.message_type === 'BLOCKER' ? 'border-amber-600/60 ring-1 ring-amber-500/20' :
            msg.message_type === 'BUG_REPORT' ? 'border-red-700/60' : ''
          }`}>
            {/* Header line */}
            <div className="flex items-center gap-2 mb-1">
              <Icon className={`w-3.5 h-3.5 shrink-0 ${config.color}`} />
              <span className={`text-[11px] font-semibold ${config.color}`}>{config.label}</span>
              <span className="text-[11px] text-neutral-600">
                {formatFromType(msg)} · {formatAge(msg.created_at)}
              </span>
            </div>
            {/* Reply context for feedback acknowledgment */}
            {replyContextMsg && (
              <div className="mb-1.5 px-2 py-1 border-l-2 border-neutral-700/50 rounded-r bg-neutral-800/30">
                <p className="text-[11px] text-neutral-500 mb-0.5">In reply to your feedback:</p>
                <p className="text-[11px] text-neutral-400 truncate">{replyContextMsg.content}</p>
              </div>
            )}
            {/* Reply quote (if this message is a reply) */}
            {typeof msg.context.reply_preview === 'string' && (
              <div className="mb-1.5 px-2 py-1 border-l-2 border-neutral-700/50 rounded-r bg-neutral-800/30">
                <p className="text-[11px] text-neutral-500 truncate">{msg.context.reply_preview}</p>
              </div>
            )}
            {/* Content */}
            <p className="text-sm text-neutral-200 leading-relaxed whitespace-pre-wrap">{msg.content}</p>
            {/* Context badges */}
            <ContextBadges context={msg.context} fromType={msg.from_type} />
          </div>

          {/* Resolution (for resolved messages) */}
          {msg.status === 'resolved' && msg.resolution && (
            <div className={`mt-1 ${userMsg ? 'mr-3' : 'ml-3'} px-3 py-1.5 rounded-lg bg-neutral-800/50 border border-neutral-700/30`}>
              <p className="text-[11px] text-neutral-500">
                <span className="text-emerald-500/70 font-medium">Resolved:</span> {msg.resolution}
              </p>
            </div>
          )}

          {/* Action buttons (open messages only, not faded, not read-only grouped rows) */}
          {!faded && !readOnly && msg.status === 'open' && (
            <div className={`flex items-center gap-1 mt-1 ${userMsg ? 'self-end mr-1' : 'self-start ml-1'}`}>
              {selectMode && msg.message_type === 'FEEDBACK' && (
                <button
                  onClick={() => toggleSelect(msg.id)}
                  className={`p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center ${
                    selectedIds.has(msg.id) ? 'text-purple-400 bg-purple-500/20' : 'text-neutral-600 hover:text-neutral-400'
                  }`}
                >
                  <CheckSquare className="w-3.5 h-3.5" />
                </button>
              )}
              <button
                onClick={() => {
                  setReplyTo({ id: msg.id, content: msg.content, from: formatFromType(msg) });
                  textareaRef.current?.focus();
                }}
                title="Reply"
                className="text-neutral-600 hover:text-blue-400 p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <Reply className="w-3.5 h-3.5" />
              </button>
              {msg.message_type === 'FEEDBACK' && !selectMode && (
                <button
                  onClick={() => handleCreateDirection(msg)}
                  disabled={creatingDirectionIds.has(msg.id)}
                  title="Create Direction"
                  className="text-purple-500/70 hover:text-purple-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                >
                  <PlusCircle className="w-3.5 h-3.5" />
                </button>
              )}
              {!isResolving ? (
                <button
                  onClick={() => { setResolvingId(msg.id); setResolveInput(''); }}
                  title="Resolve"
                  className="text-emerald-500/70 hover:text-emerald-400 p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                >
                  <Check className="w-3.5 h-3.5" />
                </button>
              ) : (
                <div className="flex items-center gap-1 bg-neutral-900 border border-neutral-700 rounded-lg px-2 py-1 ml-1">
                  <input
                    type="text"
                    value={resolveInput}
                    onChange={e => setResolveInput(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleResolve(msg.id, resolveInput); if (e.key === 'Escape') setResolvingId(null); }}
                    placeholder="Note (optional)..."
                    className="bg-transparent text-neutral-300 text-xs focus:outline-none w-36 md:w-48 placeholder-neutral-600"
                    autoFocus
                  />
                  <button
                    onClick={() => handleResolve(msg.id, resolveInput)}
                    className="text-emerald-400 hover:text-emerald-300 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                    title="Confirm resolve"
                  >
                    <Check className="w-3 h-3" />
                  </button>
                  <button
                    onClick={() => setResolvingId(null)}
                    className="text-neutral-500 hover:text-neutral-400 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                    title="Cancel"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              )}
              <button
                onClick={() => handleDelete(msg.id)}
                disabled={deletingIds.has(msg.id)}
                title="Delete"
                className="text-neutral-600 hover:text-red-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-hidden flex flex-col min-h-0">
      {/* Header */}
      <div className="px-4 md:px-6 py-3 border-b border-neutral-800 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2">
          <MessageSquare className="w-4 h-4 text-neutral-400" />
          <h2 className="text-sm font-semibold text-neutral-100">Messages</h2>
          {highSignalOpen.length > 0 && (
            <span className="bg-amber-500/20 text-amber-400 border border-amber-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
              {highSignalOpen.length} high
            </span>
          )}
          {openMessages.length > 0 && (
            <span className="bg-blue-500/20 text-blue-400 border border-blue-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
              {openMessages.length}
            </span>
          )}
          {mutedCount > 0 && (
            <button onClick={() => setShowPrefsPanel(true)} className="text-[11px] text-neutral-600 hover:text-neutral-400 transition-colors">
              {mutedCount} muted
            </button>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setSortMode(m => m === 'importance' ? 'recent' : 'importance')}
            title="Toggle sort order"
            className="text-xs px-2 py-1 rounded-lg transition-colors border border-neutral-800/60 text-neutral-500 hover:text-neutral-300 min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center"
          >
            {sortMode === 'importance' ? 'Importance' : 'Recent'}
          </button>
          {openFeedbackMessages.length > 1 && (
            <button
              onClick={() => {
                if (selectMode) { setSelectMode(false); setSelectedIds(new Set()); }
                else { setSelectMode(true); setSelectedIds(new Set(openFeedbackMessages.map(m => m.id))); }
              }}
              className={`text-xs px-2.5 py-1.5 rounded-lg transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center ${
                selectMode ? 'text-purple-400 bg-purple-500/10 border border-purple-500/30' : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
              }`}
            >
              {selectMode ? 'Cancel' : 'Select'}
            </button>
          )}
          <div className="relative">
            <button
              onClick={() => setShowPrefsPanel(v => !v)}
              title="Event type visibility"
              className={`p-2 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-[36px] flex items-center justify-center ${showPrefsPanel ? 'text-neutral-300 bg-neutral-800 border-neutral-700' : 'text-neutral-500 hover:text-neutral-300 border-transparent'}`}
            >
              <Settings className="w-4 h-4" />
            </button>
            {showPrefsPanel && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setShowPrefsPanel(false)} />
                <div className="absolute right-0 top-full mt-1 z-50 w-60 bg-neutral-900 border border-neutral-700 rounded-xl shadow-xl p-3 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-neutral-300">Event visibility</span>
                    {Object.keys(eventPrefs).length > 0 && (
                      <button onClick={() => { setEventPrefs({}); localStorage.removeItem(EVENT_PREFS_KEY); }} className="text-[10px] text-neutral-500 hover:text-red-400 transition-colors">Reset all</button>
                    )}
                  </div>
                  {allEventTypes.length === 0 ? (
                    <p className="text-[11px] text-neutral-600 py-1">No events loaded</p>
                  ) : allEventTypes.map(et => {
                    const pref = eventPrefs[et] ?? 'routine';
                    return (
                      <div key={et} className="flex items-center gap-1.5">
                        <span className="text-[11px] text-neutral-400 truncate flex-1">{et.replace(/_/g, ' ')}</span>
                        {(['surfaced', 'routine', 'mute'] as EventPref[]).map(p => (
                          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                          <button key={p} onClick={() => updatePref(et, p)}
                            title={p}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${pref === p ? (p === 'mute' ? 'border-red-500/50 text-red-400 bg-red-500/10' : p === 'surfaced' ? 'border-amber-500/50 text-amber-400 bg-amber-500/10' : 'border-neutral-600 text-neutral-300 bg-neutral-800') : 'border-transparent text-neutral-600 hover:text-neutral-400'}`}>
                            {p === 'surfaced' ? '↑' : p === 'mute' ? '✕' : '·'}
                          </button>
                        ))}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
          <button onClick={onClose} className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-2 rounded-lg text-xs transition-colors border border-neutral-700 min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-[36px] flex items-center justify-center">
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Messages list — chat-style scroll area */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 md:px-6 py-4 space-y-3 relative">
        {loading ? (
          <p className="text-xs text-neutral-500 text-center py-8">Loading...</p>
        ) : messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-neutral-500">
            <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
            <p className="text-sm">No messages yet</p>
            <p className="text-xs mt-1 opacity-70">Send feedback, report bugs, or ask questions</p>
          </div>
        ) : (
          <>
            {/* Pinned BLOCKERs (importance mode only) */}
            {pinnedBlockers.length > 0 && (
              <>
                <div className="flex items-center gap-1.5 text-[11px] text-amber-400/70 font-semibold uppercase tracking-wide px-1">
                  <AlertTriangle className="w-3 h-3" />
                  <span>Pinned · Blockers</span>
                </div>
                {pinnedBlockers.map(msg => renderBubble(msg, false, false, messages))}
                {otherHighSignal.length > 0 && <div className="border-t border-neutral-800/60" />}
              </>
            )}
            {/* Other high-signal open messages */}
            {otherHighSignal.map(msg => renderBubble(msg, false, false, messages))}

            {/* Auto-promoted failing routine groups */}
            {promotedGroups.map(row => {
              const bd = outcomeBreakdown(row.msgs);
              const isGroupExpanded = expandedGroupKeys.has(row.key);
              return (
                <div key={row.key} className="border border-red-800/50 rounded-xl overflow-hidden bg-red-950/20">
                  <div className="flex items-center gap-2 px-4 py-2.5 bg-red-950/30 min-h-[44px]">
                    <button
                      onClick={() => setExpandedGroupKeys(prev => { const next = new Set(prev); if (next.has(row.key)) next.delete(row.key); else next.add(row.key); return next; })}
                      className="flex-1 flex items-center gap-2 text-left"
                    >
                      <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
                      <span className="text-xs text-red-300 font-medium">{row.msgs.length} {row.label}</span>
                      {bd && <span className="text-xs text-red-400">{bd}</span>}
                    </button>
                    {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                    <button onClick={() => handleResolveGroup(row.msgs)} title="Resolve all" className="text-emerald-500/70 hover:text-emerald-400 p-1 rounded transition-colors">
                      <Check className="w-3 h-3" />
                    </button>
                  </div>
                  {isGroupExpanded && (
                    <div className="space-y-2 p-2 border-t border-red-900/40">
                      {row.msgs.map(msg => renderBubble(msg, false, true, messages))}
                    </div>
                  )}
                </div>
              );
            })}

            {/* Routine ops group — collapsed by default */}
            {routineOpen.length > 0 && (
              <div className="border border-neutral-800/50 rounded-xl overflow-hidden">
                <button
                  onClick={() => setRoutineExpanded(e => !e)}
                  className="w-full flex items-center gap-2 px-4 py-2.5 bg-neutral-900/40 hover:bg-neutral-900/70 transition-colors text-left min-h-[44px]"
                >
                  <ChevronDown className={`w-3.5 h-3.5 text-neutral-600 shrink-0 transition-transform ${routineExpanded ? 'rotate-180' : ''}`} />
                  <span className="text-xs text-neutral-500 font-medium shrink-0">{routineOpen.length} routine ops</span>
                  <span className="text-[11px] text-neutral-600 truncate">{routineSummary(routineOpen)}</span>
                </button>
                {routineExpanded && (
                  <div className="space-y-3 p-3 border-t border-neutral-800/40">
                    {remainingRoutineRows.map(row => {
                      if (row.kind === 'single') return renderBubble(row.msg, false, false, messages);
                      const isGroupExpanded = expandedGroupKeys.has(row.key);
                      const bd = outcomeBreakdown(row.msgs);
                      return (
                        <div key={row.key} className="border border-neutral-800/40 rounded-xl overflow-hidden">
                          <div className="flex items-center gap-2 px-3 py-2 bg-neutral-900/30">
                            <button
                              onClick={() => setExpandedGroupKeys(prev => {
                                const next = new Set(prev);
                                if (next.has(row.key)) next.delete(row.key); else next.add(row.key);
                                return next;
                              })}
                              className="flex-1 flex items-center gap-2 text-left"
                            >
                              <ChevronDown className={`w-3 h-3 text-neutral-600 shrink-0 transition-transform ${isGroupExpanded ? 'rotate-180' : ''}`} />
                              <span className="text-xs text-neutral-500 font-medium">{row.msgs.length} {row.label}{bd ? ` ${bd}` : ''}</span>
                            </button>
                            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                            <button
                              onClick={() => handleResolveGroup(row.msgs)}
                              title="Resolve all"
                              className="text-emerald-500/70 hover:text-emerald-400 p-1 rounded transition-colors"
                            >
                              <Check className="w-3 h-3" />
                            </button>
                          </div>
                          {isGroupExpanded && (
                            <div className="space-y-2 p-2 border-t border-neutral-800/40">
                              {row.msgs.map(msg => renderBubble(msg, false, true, messages))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {/* Resolved messages (faded) */}
            {visibleResolved.map(msg => renderBubble(msg, true, false, messages))}

            {/* Resolved toggle + clear */}
            {hiddenResolvedCount > 0 && (
              <div className="flex justify-center gap-3 pt-1">
                {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                <button
                  onClick={() => setShowResolved(true)}
                  className="text-[11px] text-neutral-600 hover:text-neutral-400 transition-colors px-2 py-1"
                >
                  Show {hiddenResolvedCount} older resolved
                </button>
              </div>
            )}
            {showResolved && resolvedMessages.length > 0 && (
              <div className="flex justify-center gap-3 pt-1">
                {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                <button
                  onClick={() => setShowResolved(false)}
                  className="text-[11px] text-neutral-600 hover:text-neutral-400 transition-colors px-2 py-1"
                >
                  Hide older resolved
                </button>
                {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                <button
                  onClick={handleBulkDeleteResolved}
                  className="text-[11px] text-neutral-600 hover:text-red-400 transition-colors px-2 py-1"
                >
                  Clear all resolved
                </button>
              </div>
            )}
          </>
        )}

        {/* Scroll to bottom button */}
        {showScrollDown && (
          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
          <button
            onClick={scrollToBottom}
            className="sticky bottom-2 left-1/2 -translate-x-1/2 bg-neutral-800 border border-neutral-700 text-neutral-400 hover:text-neutral-200 p-2 rounded-full shadow-lg transition-colors z-10"
          >
            <ArrowDown className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Batch action bar */}
      {selectMode && selectedIds.size > 0 && (
        <div className="px-4 md:px-6 py-2 border-t border-neutral-800 bg-neutral-900 flex items-center gap-3 shrink-0">
          <span className="text-xs text-neutral-400">{selectedIds.size} selected</span>
          <button
            onClick={handleBatchCreateDirections}
            disabled={batchCreating}
            className="text-xs bg-purple-600 hover:bg-purple-500 text-white px-3 py-1.5 rounded-lg disabled:opacity-40 transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center"
          >
            {batchCreating ? 'Creating...' : 'Create Directions'}
          </button>
        </div>
      )}

      {/* Compose area — bottom, chat-style */}
      <div className="px-4 md:px-6 py-3 border-t border-neutral-800 shrink-0 bg-neutral-950/50">
        {/* Reply quote bar */}
        {replyTo && (
          <div className="flex items-center gap-2 mb-2 px-3 py-1.5 bg-neutral-800/60 border-l-2 border-blue-500/50 rounded-r-lg">
            <Reply className="w-3 h-3 text-blue-400 shrink-0" />
            <span className="text-xs text-neutral-400 truncate flex-1">
              Replying to <span className="text-neutral-300">{replyTo.from}</span>: {replyTo.content.slice(0, 80)}{replyTo.content.length > 80 ? '...' : ''}
            </span>
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button onClick={() => setReplyTo(null)} className="text-neutral-500 hover:text-neutral-300 p-0.5 shrink-0">
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
        <div className="flex gap-2 items-end">
          <MessageTypeDropdown value={newType} onChange={setNewType} />
          <div className="flex-1 relative">
            <textarea
              ref={textareaRef}
              value={newContent}
              onChange={e => { setNewContent(e.target.value); handleTextareaInput(); }}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
              }}
              placeholder="Type a message..."
              rows={1}
              className="w-full bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600 resize-none min-h-[44px] leading-relaxed"
            />
          </div>
          <button
            onClick={handleSubmit}
            disabled={submitting || !newContent.trim()}
            className="bg-blue-600 hover:bg-blue-500 text-white p-2.5 rounded-xl text-xs font-medium transition-colors disabled:opacity-40 shrink-0 min-h-[44px] min-w-[44px] flex items-center justify-center"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
