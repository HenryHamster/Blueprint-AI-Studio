import { useState, useEffect, useCallback, useRef } from 'react';
import { X, MessageSquare, Send, Check, AlertTriangle, HelpCircle, Bug, StickyNote, ChevronDown, ChevronRight, PlusCircle } from 'lucide-react';
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
}

const TYPE_CONFIG: Record<string, { icon: typeof MessageSquare; color: string; label: string }> = {
  QUESTION: { icon: HelpCircle, color: 'text-blue-400 bg-blue-500/10 border-blue-500/30', label: 'Question' },
  FEEDBACK: { icon: MessageSquare, color: 'text-purple-400 bg-purple-500/10 border-purple-500/30', label: 'Feedback' },
  BUG_REPORT: { icon: Bug, color: 'text-red-400 bg-red-500/10 border-red-500/30', label: 'Bug Report' },
  BLOCKER: { icon: AlertTriangle, color: 'text-amber-400 bg-amber-500/10 border-amber-500/30', label: 'Blocker' },
  NOTE: { icon: StickyNote, color: 'text-neutral-400 bg-neutral-500/10 border-neutral-500/30', label: 'Note' },
};

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

export function MessagesPanel({ selectedProjectId, onClose, addToast }: MessagesPanelProps) {
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [resolvedExpanded, setResolvedExpanded] = useState(false);

  // New message form
  const [newType, setNewType] = useState('FEEDBACK');
  const [newContent, setNewContent] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // Resolve inputs per message
  const [resolveInputs, setResolveInputs] = useState<Record<string, string>>({});
  const [resolvingIds, setResolvingIds] = useState<Set<string>>(new Set());
  const [creatingDirectionIds, setCreatingDirectionIds] = useState<Set<string>>(new Set());

  // Batch selection state
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchCreating, setBatchCreating] = useState(false);

  // Stable ref for addToast to avoid re-creating fetchMessages on every render
  const addToastRef = useRef(addToast);
  addToastRef.current = addToast;

  const fetchMessages = useCallback(async () => {
    if (!selectedProjectId) { setLoading(false); return; }
    try {
      const params = new URLSearchParams({ project_id: selectedProjectId, status: 'all', limit: '100' });
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setMessages(data.messages ?? []);
      setSelectedIds(new Set());
    } catch {
      addToastRef.current('Failed to load messages', 'error');
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId]);

  useEffect(() => {
    fetchMessages();
    const interval = setInterval(fetchMessages, 30_000);
    return () => clearInterval(interval);
  }, [fetchMessages]);

  async function handleSubmit() {
    if (!selectedProjectId || !newContent.trim()) return;
    setSubmitting(true);
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: selectedProjectId,
          message_type: newType,
          content: newContent.trim(),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setNewContent('');
      addToast('Message posted', 'success');
      await fetchMessages();
    } catch {
      addToast('Failed to post message', 'error');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResolve(msgId: string) {
    setResolvingIds(prev => new Set(prev).add(msgId));
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msgId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution: resolveInputs[msgId] || '' }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setResolveInputs(prev => Object.fromEntries(Object.entries(prev).filter(([k]) => k !== msgId)));
      await fetchMessages();
    } catch {
      addToast('Failed to resolve message', 'error');
    } finally {
      setResolvingIds(prev => { const next = new Set(prev); next.delete(msgId); return next; });
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
      // Auto-resolve the message
      const resolveRes = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msg.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution: `Direction created: ${directionId}` }),
      });
      if (!resolveRes.ok) throw new Error(`HTTP ${resolveRes.status}`);
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

  const openMessages = messages.filter(m => m.status === 'open');
  const resolvedMessages = messages.filter(m => m.status === 'resolved');
  const openFeedbackMessages = openMessages.filter(m => m.message_type === 'FEEDBACK');
  const allFeedbackSelected = openFeedbackMessages.length > 0 && openFeedbackMessages.every(m => selectedIds.has(m.id));

  function toggleSelectAllFeedback() {
    if (allFeedbackSelected) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(openFeedbackMessages.map(m => m.id)));
    }
  }

  function toggleSelect(msgId: string) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(msgId)) next.delete(msgId);
      else next.add(msgId);
      return next;
    });
  }

  async function handleBatchCreateDirections() {
    const ids = Array.from(selectedIds);
    const msgs = openFeedbackMessages.filter(m => ids.includes(m.id));
    if (msgs.length === 0) return;
    setBatchCreating(true);
    let count = 0;
    for (const msg of msgs) {
      const ok = await handleCreateDirection(msg, true);
      if (ok) count++;
    }
    setBatchCreating(false);
    setSelectedIds(new Set());
    if (count > 0) addToast(`${count} direction${count > 1 ? 's' : ''} created`, 'success');
  }

  return (
    <div className="flex-1 overflow-hidden flex flex-col min-h-0">
      {/* Header */}
      <div className="px-6 py-4 border-b border-neutral-800 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2">
          <MessageSquare className="w-4 h-4 text-neutral-400" />
          <h2 className="text-sm font-semibold text-neutral-100">Messages</h2>
          {openMessages.length > 0 && (
            <span className="bg-blue-500/20 text-blue-400 border border-blue-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
              {openMessages.length} open
            </span>
          )}
          {openFeedbackMessages.length > 1 && (
            <button onClick={toggleSelectAllFeedback} className="text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors">
              {allFeedbackSelected ? 'Deselect all' : `Select all ${openFeedbackMessages.length} feedback`}
            </button>
          )}
        </div>
        <button onClick={onClose} className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-1.5 rounded text-xs transition-colors border border-neutral-700">
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Post form */}
      <div className="px-6 py-3 border-b border-neutral-800 shrink-0">
        <div className="flex gap-2">
          <select
            value={newType}
            onChange={e => setNewType(e.target.value)}
            className="bg-neutral-900 border border-neutral-700 text-neutral-300 rounded-lg px-2.5 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 shrink-0"
          >
            <option value="FEEDBACK">Feedback</option>
            <option value="BUG_REPORT">Bug Report</option>
            <option value="NOTE">Note</option>
          </select>
          <input
            type="text"
            value={newContent}
            onChange={e => setNewContent(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); } }}
            placeholder="Type a message..."
            className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
          />
          <button
            onClick={handleSubmit}
            disabled={submitting || !newContent.trim()}
            className="bg-blue-600 hover:bg-blue-500 text-white px-3 py-2 rounded-lg text-sm transition-colors disabled:opacity-40 flex items-center gap-1.5 shrink-0"
          >
            <Send className="w-3.5 h-3.5" />
            Send
          </button>
        </div>
      </div>

      {/* Messages list */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {loading ? (
          <p className="text-xs text-neutral-500 text-center py-8">Loading...</p>
        ) : messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-neutral-500">
            <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
            <p className="text-sm">No messages yet</p>
            <p className="text-xs mt-1 opacity-70">Post feedback, bug reports, or questions</p>
          </div>
        ) : (
          <>
            {/* Open messages */}
            {openMessages.map(msg => {
              const config = TYPE_CONFIG[msg.message_type] || TYPE_CONFIG.NOTE;
              const Icon = config.icon;
              const borderColor = msg.message_type === 'BUG_REPORT' ? 'border-l-red-500' : msg.message_type === 'QUESTION' ? 'border-l-blue-500' : msg.message_type === 'BLOCKER' ? 'border-l-amber-500' : msg.message_type === 'FEEDBACK' ? 'border-l-purple-500' : 'border-l-neutral-600';
              return (
                <div key={msg.id} className={`bg-neutral-900 border border-neutral-800 border-l-4 rounded-lg overflow-hidden ${borderColor}`}>
                  <div className="px-4 py-3">
                    <div className="flex items-start gap-3">
                      {msg.message_type === 'FEEDBACK' && (
                        <input
                          type="checkbox"
                          checked={selectedIds.has(msg.id)}
                          onChange={() => toggleSelect(msg.id)}
                          className="mt-1 shrink-0 accent-purple-500 cursor-pointer"
                        />
                      )}
                      <Icon className={`w-4 h-4 shrink-0 mt-0.5 ${config.color.split(' ')[0]}`} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1.5">
                          <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${config.color}`}>
                            {config.label}
                          </span>
                          <span className="text-[10px] text-neutral-600">
                            {msg.from_type === 'AGENT' ? 'Agent' : 'User'} · {formatAge(msg.created_at)}
                          </span>
                          {msg.from_task_id && (
                            <span className="text-[10px] text-neutral-600 font-mono">
                              task {msg.from_task_id.slice(0, 8)}
                            </span>
                          )}
                        </div>
                        <p className="text-sm text-neutral-200 leading-relaxed">{msg.content}</p>
                      </div>
                    </div>
                  </div>
                  <div className="flex gap-2 px-4 py-2.5 bg-neutral-950/50 border-t border-neutral-800/50">
                    <input
                      type="text"
                      value={resolveInputs[msg.id] || ''}
                      onChange={e => setResolveInputs(prev => ({ ...prev, [msg.id]: e.target.value }))}
                      onKeyDown={e => { if (e.key === 'Enter') handleResolve(msg.id); }}
                      placeholder={msg.message_type === 'QUESTION' ? 'Your answer...' : 'Resolution note (optional)...'}
                      className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-300 rounded-lg px-2.5 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
                    />
                    {msg.message_type === 'FEEDBACK' && (
                      <button
                        onClick={() => handleCreateDirection(msg)}
                        disabled={creatingDirectionIds.has(msg.id)}
                        title="Create Direction from Feedback"
                        className="bg-purple-700 hover:bg-purple-600 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-40 flex items-center gap-1"
                      >
                        <PlusCircle className="w-3 h-3" />
                        Create Direction
                      </button>
                    )}
                    <button
                      onClick={() => handleResolve(msg.id)}
                      disabled={resolvingIds.has(msg.id)}
                      className="bg-emerald-600 hover:bg-emerald-500 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-40 flex items-center gap-1"
                    >
                      <Check className="w-3 h-3" />
                      Resolve
                    </button>
                  </div>
                </div>
              );
            })}

            {/* Batch action bar */}
            {selectedIds.size > 0 && (
              <div className="sticky bottom-0 bg-neutral-800 border-t border-neutral-700 px-4 py-2 flex items-center gap-3 rounded-lg">
                <span className="text-xs text-neutral-400">{selectedIds.size} selected</span>
                <button
                  onClick={handleBatchCreateDirections}
                  disabled={batchCreating}
                  className="text-xs bg-purple-600 hover:bg-purple-500 text-white px-3 py-1 rounded disabled:opacity-40 transition-colors"
                >
                  {batchCreating ? 'Creating...' : 'Create Directions'}
                </button>
                <button
                  onClick={() => setSelectedIds(new Set())}
                  className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
                >
                  Cancel
                </button>
              </div>
            )}

            {/* Resolved messages */}
            {resolvedMessages.length > 0 && (
              <div className="pt-2">
                <button
                  onClick={() => setResolvedExpanded(prev => !prev)}
                  className="flex items-center gap-1.5 text-xs text-neutral-500 hover:text-neutral-400 transition-colors"
                >
                  {resolvedExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                  {resolvedMessages.length} resolved
                </button>
                {resolvedExpanded && (
                  <div className="mt-2 space-y-1.5">
                    {resolvedMessages.map(msg => {
                      const config = TYPE_CONFIG[msg.message_type] || TYPE_CONFIG.NOTE;
                      return (
                        <div key={msg.id} className="bg-neutral-950 border border-neutral-800/50 rounded px-3 py-2 opacity-60">
                          <div className="flex items-center gap-2 text-xs">
                            <span className={`text-[9px] px-1 py-0.5 rounded ${config.color}`}>{config.label}</span>
                            <span className="text-neutral-500 truncate flex-1">{msg.content}</span>
                            <span className="text-neutral-600 text-[10px] shrink-0">{formatAge(msg.resolved_at || msg.created_at)}</span>
                          </div>
                          {msg.resolution && (
                            <p className="text-[10px] text-neutral-500 mt-1 ml-1">Response: {msg.resolution}</p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
