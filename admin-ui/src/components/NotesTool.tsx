import { useCallback, useEffect, useRef, useState } from 'react';
import type { ToolComponentProps } from '../toolRegistry';
import { fetchToolContent, saveToolContent, ConflictError, type ToolContent } from '../api/projectTools';
import MarkdownContentInner from './MarkdownContentInner';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// The persisted content shape: a single Markdown string in the tool's JSONB
// content column. Kept intentionally minimal — the whole document is the text.
interface NotesContent {
  text: string;
}

type State =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'empty' }
  | { status: 'ok'; text: string; revision: number };

type Mode = 'edit' | 'preview';

const DEBOUNCE_MS = 1500;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function NotesTool({ projectId, tool }: ToolComponentProps) {
  const [state, setState] = useState<State>({ status: 'loading' });
  const [mode, setMode] = useState<Mode>('edit');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // A conflict surfaced by a 409 on save: another viewer committed a newer
  // revision. We keep the LOCAL edits intact and let the operator reconcile.
  const [conflict, setConflict] = useState<{ serverText: string; serverRevision: number } | null>(null);

  // Latest text/revision refs so the debounced saver always reads fresh values
  // without re-binding on every keystroke (same pattern as WhiteboardTool).
  const textRef = useRef('');
  const revisionRef = useRef(0);
  // Serialized snapshot of the last content we successfully persisted (or
  // loaded). The autosave effect compares CONTENT to this — not state object
  // identity — so a save-triggered revision refresh never re-fires a save.
  const lastSavedRef = useRef<string>('');

  // Keep refs in sync with the committed state.
  useEffect(() => {
    if (state.status === 'ok') {
      textRef.current = state.text;
      revisionRef.current = state.revision;
    }
  }, [state]);

  // -----------------------------------------------------------------------
  // Load content on mount
  // -----------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data: ToolContent<NotesContent> = await fetchToolContent<NotesContent>(
          projectId,
          tool.id,
        );
        if (cancelled) return;
        const text = data.content?.text ?? '';
        textRef.current = text;
        revisionRef.current = data.content_revision;
        lastSavedRef.current = JSON.stringify({ text });
        if (!text) {
          setState({ status: 'empty' });
        } else {
          setState({ status: 'ok', text, revision: data.content_revision });
        }
      } catch (err) {
        if (cancelled) return;
        setState({
          status: 'error',
          message: err instanceof Error ? err.message : 'Failed to load notes',
        });
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectId, tool.id]);

  // -----------------------------------------------------------------------
  // Editing
  // -----------------------------------------------------------------------

  const handleChange = useCallback((value: string) => {
    textRef.current = value;
    setState(prev => {
      // Promote empty → ok on the first keystroke so autosave can begin.
      if (prev.status === 'empty') {
        return { status: 'ok', text: value, revision: revisionRef.current };
      }
      if (prev.status === 'ok') {
        return { ...prev, text: value };
      }
      return prev;
    });
  }, []);

  // -----------------------------------------------------------------------
  // Save
  // -----------------------------------------------------------------------

  const doSave = useCallback(async () => {
    if (state.status !== 'ok') return;
    // Snapshot the exact payload we are about to persist. On success this
    // becomes the "last saved" baseline the autosave effect diffs against.
    const payload = { text: textRef.current };
    const serialized = JSON.stringify(payload);
    setSaving(true);
    setSaveError(null);
    try {
      const result = await saveToolContent(
        projectId,
        tool.id,
        payload,
        revisionRef.current,
      );
      revisionRef.current = result.content_revision;
      lastSavedRef.current = serialized;
      setConflict(null);
      setState(prev => (prev.status === 'ok' ? { ...prev, revision: result.content_revision } : prev));
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Save failed';
      setSaveError(msg);
      // On a 409 conflict, PRESERVE the local edits — never silently discard a
      // losing writer's work. Fetch the server's version so the operator can
      // reconcile, adopt the server revision as the new save base (so a
      // deliberate "keep mine" retry can land), and pause autosave until the
      // operator resolves the conflict.
      if (err instanceof ConflictError) {
        try {
          const data: ToolContent<NotesContent> = await fetchToolContent<NotesContent>(projectId, tool.id);
          const serverText = data.content?.text ?? '';
          revisionRef.current = data.content_revision;
          setConflict({ serverText, serverRevision: data.content_revision });
        } catch {
          // If reload fails too, keep the existing (local) state and error.
        }
      }
    } finally {
      setSaving(false);
    }
  }, [state.status, projectId, tool.id]);

  // Keep my local text; overwrite the server with it (base revision was already
  // advanced to the server's during conflict handling, so this retry lands).
  const resolveKeepMine = useCallback(() => {
    setConflict(null);
    void doSave();
  }, [doSave]);

  // Discard my local text in favour of the server's version — an EXPLICIT
  // operator choice, not a silent drop.
  const resolveUseTheirs = useCallback(() => {
    const c = conflict;
    if (!c) return;
    textRef.current = c.serverText;
    revisionRef.current = c.serverRevision;
    lastSavedRef.current = JSON.stringify({ text: c.serverText });
    setConflict(null);
    setSaveError(null);
    setState(c.serverText
      ? { status: 'ok', text: c.serverText, revision: c.serverRevision }
      : { status: 'empty' });
  }, [conflict]);

  // Auto-save after a pause in typing (debounced). Fires only when the CONTENT
  // actually differs from the last-saved snapshot (not on a bare revision
  // refresh), and never while a conflict awaits operator resolution.
  useEffect(() => {
    if (state.status !== 'ok') return;
    if (conflict) return;
    if (JSON.stringify({ text: state.text }) === lastSavedRef.current) return;
    const timer = setTimeout(() => {
      void doSave();
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [state, doSave, conflict]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (state.status === 'loading') {
    return (
      <div className="flex items-center gap-2 text-sm text-neutral-500 py-4">
        <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-neutral-600 border-t-neutral-300" />
        Loading notes…
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className="rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
        {state.message}
      </div>
    );
  }

  const text = state.status === 'ok' ? state.text : '';

  return (
    <div className="flex flex-col gap-2">
      {/* Toolbar: edit/preview toggle + save status */}
      <div className="flex flex-wrap items-center gap-1.5" data-viewport-allow-overflow data-viewport-allow-small-target>
        <div className="flex items-center gap-0.5" role="radiogroup" aria-label="View mode">
          <button
            type="button"
            role="radio"
            aria-checked={mode === 'edit'}
            aria-label="Edit mode"
            onClick={() => setMode('edit')}
            className={`min-h-[44px] rounded border px-2 py-1 text-xs transition-colors ${
              mode === 'edit'
                ? 'border-white bg-neutral-700 text-neutral-100'
                : 'border-neutral-600 text-neutral-400 hover:border-neutral-400'
            }`}
          >
            Edit
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={mode === 'preview'}
            aria-label="Preview mode"
            onClick={() => setMode('preview')}
            className={`min-h-[44px] rounded border px-2 py-1 text-xs transition-colors ${
              mode === 'preview'
                ? 'border-white bg-neutral-700 text-neutral-100'
                : 'border-neutral-600 text-neutral-400 hover:border-neutral-400'
            }`}
          >
            Preview
          </button>
        </div>

        {/* Save status */}
        <span className="ml-auto text-xs text-neutral-500">
          {saving ? 'Saving…' : conflict ? 'Conflict' : saveError ? 'Save failed' : 'Saved'}
        </span>
      </div>

      {/* Conflict banner: another viewer saved a newer revision. Local edits
          are preserved above; the operator explicitly picks a resolution. */}
      {conflict && (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-300" role="alert">
          <p className="font-medium">Another viewer saved a newer version while you were editing.</p>
          <p className="mt-1 text-amber-400/80">Your unsaved edits are kept above. Choose how to resolve:</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={resolveKeepMine}
              className="min-h-[44px] rounded border border-amber-500/50 px-2 py-1 hover:bg-amber-500/20"
            >
              Keep mine (overwrite theirs)
            </button>
            <button
              type="button"
              onClick={resolveUseTheirs}
              className="min-h-[44px] rounded border border-neutral-600 px-2 py-1 text-neutral-300 hover:border-neutral-400"
            >
              Discard mine, load theirs
            </button>
          </div>
        </div>
      )}

      {/* Editor / preview */}
      {mode === 'edit' ? (
        <textarea
          value={text}
          onChange={e => handleChange(e.target.value)}
          placeholder="Write Markdown here. LaTeX renders in preview, e.g. $E = mc^2$"
          aria-label="Notes markdown editor"
          spellCheck={false}
          className="min-h-[16rem] w-full resize-y rounded border border-neutral-700 bg-[#1a1a2e] p-3 font-mono text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-neutral-500 focus:outline-none"
        />
      ) : (
        <div
          className="min-h-[16rem] w-full overflow-x-auto rounded border border-neutral-700 bg-[#1a1a2e] p-3"
          data-viewport-allow-overflow
        >
          {text ? (
            <MarkdownContentInner content={text} />
          ) : (
            <p className="text-sm italic text-neutral-500">Nothing to preview yet.</p>
          )}
        </div>
      )}

      {/* Save error detail with retry */}
      {saveError && (
        <p className="text-xs text-amber-400">
          {saveError}
          <button
            type="button"
            onClick={() => { setSaveError(null); void doSave(); }}
            className="ml-1 underline hover:text-amber-300"
          >
            Retry
          </button>
        </p>
      )}
    </div>
  );
}
