import { useState, useRef, useEffect, useCallback } from 'react';
import { X, Mail } from 'lucide-react';
import { apiFetch, API_BASE_URL } from '../api';
import { MessagesComposer } from './MessagesComposer';
import { parseArmingOutcome, armingToastCopy, type ArmingOutcome } from './messageArming';
import { generateIdempotencyKey } from '../utils/idempotencyKey';
import { timeAgo } from '../utils/time';

function draftKey(userId: string | null, projectId: string | null): string {
  return `messages_composer_draft:${userId || 'anonymous'}:${projectId || 'unknown'}`;
}

function loadDraft(userId: string | null, projectId: string | null): { type: string; content: string } | null {
  try {
    const raw = localStorage.getItem(draftKey(userId, projectId));
    if (!raw) return null;
    const p = JSON.parse(raw) as { type?: unknown; content?: unknown };
    if (typeof p.type !== 'string' || typeof p.content !== 'string') return null;
    return { type: p.type, content: p.content };
  } catch { return null; }
}

interface SentMessage {
  id: string;
  content: string;
  message_type: string;
  created_at: string | null;
}

interface ComposeDrawerProps {
  projectId: string | null;
  userEmail: string;
  onOpenFullPanel: () => void;
  onClose: () => void;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  strategistActive?: boolean;
}

export function ComposeDrawer({
  projectId,
  userEmail,
  onOpenFullPanel,
  onClose,
  addToast,
  strategistActive,
}: ComposeDrawerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const submittingRef = useRef(false);
  const idempotencyKeyRef = useRef<string | null>(null);

  const [newType, setNewType] = useState(() => loadDraft(userEmail, projectId)?.type ?? 'FEEDBACK');
  const [newContent, setNewContent] = useState(() => loadDraft(userEmail, projectId)?.content ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [lastArming, setLastArming] = useState<ArmingOutcome | null>(null);
  const [recentMessages, setRecentMessages] = useState<SentMessage[]>([]);

  useEffect(() => {
    try {
      if (newContent.trim()) {
        localStorage.setItem(draftKey(userEmail, projectId), JSON.stringify({ type: newType, content: newContent }));
      } else {
        localStorage.removeItem(draftKey(userEmail, projectId));
      }
    } catch { /* ignore */ }
  }, [newContent, newType, userEmail, projectId]);

  useEffect(() => {
    if (!projectId) return;
    const pid = projectId;
    apiFetch(`${API_BASE_URL}/api/admin/messages?project_id=${encodeURIComponent(pid)}&status=open&limit=50`)
      .then(async res => {
        if (!res.ok) return;
        const data = await res.json() as { messages?: Array<{ id: string; content: string; message_type: string; created_at: string | null; from_type: string }> };
        const sent = (data.messages ?? [])
          .filter(m => (m.from_type || '').toLowerCase() === 'user')
          .slice(0, 3);
        setRecentMessages(sent.map(m => ({ id: m.id, content: m.content, message_type: m.message_type, created_at: m.created_at })));
      })
      .catch(() => {});
  }, [projectId]);

  const handleSubmit = useCallback(async () => {
    if (!projectId || !newContent.trim() || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      if (!idempotencyKeyRef.current) {
        idempotencyKeyRef.current = generateIdempotencyKey();
      }
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: projectId,
          message_type: newType,
          content: newContent.trim(),
          idempotency_key: idempotencyKeyRef.current,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      let arming: ArmingOutcome | null = null;
      try {
        const data = await res.json() as { arming?: unknown };
        arming = parseArmingOutcome(data?.arming);
      } catch { /* ignore */ }
      idempotencyKeyRef.current = null;
      setNewContent('');
      try { localStorage.removeItem(draftKey(userEmail, projectId)); } catch { /* ignore */ }
      setLastArming(arming);
      if (textareaRef.current) textareaRef.current.style.height = 'auto';
      addToast(armingToastCopy(arming), arming === 'not_armed' ? 'info' : 'success');
    } catch {
      addToast('Failed to send message', 'error');
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }, [projectId, newType, newContent, userEmail, addToast]);

  return (
    <div className="fixed inset-x-0 bottom-0 z-50 flex flex-col bg-neutral-950 border-t border-neutral-800 shadow-2xl rounded-t-xl max-h-[60vh]">
      <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800 shrink-0">
        <div className="flex items-center gap-2">
          <Mail className="w-4 h-4 text-neutral-400" />
          <h2 className="text-sm font-semibold text-neutral-200">Send Message</h2>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={() => { onClose(); onOpenFullPanel(); }}
            className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
          >
            View all messages
          </button>
          {/* eslint-disable-next-line viewport/min-touch-target */}
          <button
            onClick={onClose}
            className="text-neutral-400 hover:text-neutral-200 transition-colors p-1"
            title="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {recentMessages.length > 0 && (
        <div className="px-4 py-2 flex flex-col gap-1.5 overflow-y-auto shrink-0 border-b border-neutral-800/60">
          <span className="text-ui-sm text-neutral-500 uppercase tracking-wide font-medium">Recent</span>
          {recentMessages.map(msg => (
            <button
              key={msg.id}
              type="button"
              onClick={() => { setNewType(msg.message_type); setNewContent(msg.content); }}
              className="flex items-start gap-2 w-full text-left hover:bg-neutral-800/50 rounded px-1 py-0.5 transition-colors min-h-[44px]"
            >
              <span className="text-ui-sm px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 font-medium shrink-0 uppercase">{msg.message_type}</span>
              <span className="text-xs text-neutral-300 truncate flex-1">{msg.content}</span>
              {msg.created_at && (
                <span className="text-ui-sm text-neutral-600 shrink-0">{timeAgo(msg.created_at)}</span>
              )}
            </button>
          ))}
        </div>
      )}

      <MessagesComposer
        textareaRef={textareaRef}
        newType={newType}
        newContent={newContent}
        submitting={submitting}
        replyTo={null}
        lastArming={lastArming}
        strategistBusy={strategistActive}
        onChangeType={setNewType}
        onChangeContent={setNewContent}
        onCancelReply={() => {}}
        onSubmit={() => { void handleSubmit(); }}
      />
    </div>
  );
}
