import { useState, useEffect, useCallback, useRef } from 'react';
import { X, MessageSquare, Send, Check, AlertTriangle, HelpCircle, Bug, StickyNote, ChevronDown, PlusCircle, Trash2, CheckSquare, ArrowDown, Reply } from 'lucide-react';
import { apiFetch, API_BASE_URL } from '../api';
import { parseTimestamp } from '../utils/time';

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

  // Collapsed resolved + "show older" for mobile perf
  const [showResolved, setShowResolved] = useState(false);
  const INITIAL_VISIBLE = 20;
  const [visibleCount, setVisibleCount] = useState(INITIAL_VISIBLE);

  // Stable ref for addToast
  const addToastRef = useRef(addToast);
  addToastRef.current = addToast;

  // Sort ALL messages chronologically (oldest first for chat layout)
  const sortedMessages = [...messages].sort((a, b) => {
    const ta = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
    const tb = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
    return ta - tb;
  });

  const openMessages = messages.filter(m => m.status === 'open');
  const resolvedMessages = messages.filter(m => m.status === 'resolved');
  const openFeedbackMessages = openMessages.filter(m => m.message_type === 'FEEDBACK');

  // Show all open + last few resolved for context; toggle to show all resolved
  const RECENT_RESOLVED = 5;
  const sortedResolved = [...resolvedMessages].sort((a, b) => {
    const ta = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
    const tb = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
    return ta - tb;
  });
  const recentResolvedIds = new Set(
    showResolved ? resolvedMessages.map(m => m.id)
      : sortedResolved.slice(-RECENT_RESOLVED).map(m => m.id)
  );
  const hiddenResolvedCount = showResolved ? 0 : Math.max(0, resolvedMessages.length - RECENT_RESOLVED);
  const filteredMessages = sortedMessages.filter(m => m.status === 'open' || recentResolvedIds.has(m.id));
  const hiddenOlderCount = Math.max(0, filteredMessages.length - visibleCount);
  const visibleMessages = hiddenOlderCount > 0 ? filteredMessages.slice(hiddenOlderCount) : filteredMessages;

  const fetchMessages = useCallback(async () => {
    if (!selectedProjectId) { setLoading(false); return; }
    try {
      const params = new URLSearchParams({ project_id: selectedProjectId, status: 'all', limit: '100' });
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const msgs: ProjectMessage[] = data.messages ?? [];
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
  }, [selectedProjectId, onMessageCountChange]);

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
    const currentCount = visibleMessages.length;
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
  }, [visibleMessages]);

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

  function renderBubble(msg: ProjectMessage, faded = false) {
    const config = TYPE_CONFIG[msg.message_type] || TYPE_CONFIG.NOTE;
    const Icon = config.icon;
    const userMsg = isUserMessage(msg);
    const isResolving = resolvingId === msg.id;

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

          {/* Action buttons (open messages only, not faded) */}
          {!faded && msg.status === 'open' && (
            <div className={`flex items-center gap-1 mt-1 ${userMsg ? 'self-end mr-1' : 'self-start ml-1'}`}>
              {selectMode && msg.message_type === 'FEEDBACK' && (
                <button
                  onClick={() => toggleSelect(msg.id)}
                  className={`p-1.5 rounded transition-colors ${
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
                className="text-neutral-600 hover:text-blue-400 p-1.5 rounded transition-colors"
              >
                <Reply className="w-3.5 h-3.5" />
              </button>
              {msg.message_type === 'FEEDBACK' && !selectMode && (
                <button
                  onClick={() => handleCreateDirection(msg)}
                  disabled={creatingDirectionIds.has(msg.id)}
                  title="Create Direction"
                  className="text-purple-500/70 hover:text-purple-400 p-1.5 rounded transition-colors disabled:opacity-30"
                >
                  <PlusCircle className="w-3.5 h-3.5" />
                </button>
              )}
              {!isResolving ? (
                <button
                  onClick={() => { setResolvingId(msg.id); setResolveInput(''); }}
                  title="Resolve"
                  className="text-emerald-500/70 hover:text-emerald-400 p-1.5 rounded transition-colors"
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
                    className="text-emerald-400 hover:text-emerald-300 p-1 rounded transition-colors"
                    title="Confirm resolve"
                  >
                    <Check className="w-3 h-3" />
                  </button>
                  <button
                    onClick={() => setResolvingId(null)}
                    className="text-neutral-500 hover:text-neutral-400 p-1 rounded transition-colors"
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
                className="text-neutral-600 hover:text-red-400 p-1.5 rounded transition-colors disabled:opacity-30"
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
          {openMessages.length > 0 && (
            <span className="bg-blue-500/20 text-blue-400 border border-blue-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
              {openMessages.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {openFeedbackMessages.length > 1 && (
            <button
              onClick={() => {
                if (selectMode) { setSelectMode(false); setSelectedIds(new Set()); }
                else { setSelectMode(true); setSelectedIds(new Set(openFeedbackMessages.map(m => m.id))); }
              }}
              className={`text-xs px-2.5 py-1.5 rounded-lg transition-colors min-h-[36px] ${
                selectMode ? 'text-purple-400 bg-purple-500/10 border border-purple-500/30' : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
              }`}
            >
              {selectMode ? 'Cancel' : 'Select'}
            </button>
          )}
          <button onClick={onClose} className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-2 rounded-lg text-xs transition-colors border border-neutral-700 min-h-[36px] min-w-[36px] flex items-center justify-center">
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
            {/* Load older messages button */}
            {hiddenOlderCount > 0 && (
              <div className="flex justify-center pb-2">
                <button
                  onClick={() => setVisibleCount(c => c + 20)}
                  className="text-xs text-neutral-500 hover:text-neutral-300 bg-neutral-800/50 border border-neutral-700/50 px-3 py-1.5 rounded-lg transition-colors"
                >
                  Load {Math.min(hiddenOlderCount, 20)} older messages ({hiddenOlderCount} hidden)
                </button>
              </div>
            )}

            {/* Visible messages — resolved ones are faded */}
            {visibleMessages.map(msg => renderBubble(msg, msg.status === 'resolved'))}

            {/* Resolved toggle + clear */}
            {hiddenResolvedCount > 0 && (
              <div className="flex justify-center gap-3 pt-1">
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
                <button
                  onClick={() => setShowResolved(false)}
                  className="text-[11px] text-neutral-600 hover:text-neutral-400 transition-colors px-2 py-1"
                >
                  Hide older resolved
                </button>
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
            className="text-xs bg-purple-600 hover:bg-purple-500 text-white px-3 py-1.5 rounded-lg disabled:opacity-40 transition-colors min-h-[36px]"
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
