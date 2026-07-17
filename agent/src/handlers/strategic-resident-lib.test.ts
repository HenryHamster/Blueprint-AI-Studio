import { describe, it, expect } from 'vitest';
import {
  residentConfig, initialCursor, advanceCursorFromMessages,
  BoundedSeenSet, dedupNewMessages, extractMessageTurn, combineOperatorMessages,
  materializeContentBlocks, isAllowedAttachmentDownloadUrl,
  buildBootstrapPrompt, buildAutonomousPrompt,
  resolveTerminalStatus, nextAuthorityHeld, autonomousActive, FrameCoalescer,
  buildPriorDecisionsText,
  buildRetiredContextText,
  buildStagedDirectionsText, formatDirectionGroundingText,
  MESSAGE_POLL_LIMIT,
  buildSurfaceCatalog, nextFocus, parseTickVerdict, parseAgendaContinue,
  parseOperatorDecision, OPERATOR_DECISION_VERDICT,
  mergePendingOperatorDecisionReview, advancePendingOperatorDecisionReview,
  retainPendingOperatorDecisionReview, MAX_REVIEW_ATTEMPTS,
  classifyOperatorDecisionReview,
  openFoundLeads, classifyTickOutcome, shouldRunAutonomousTurn,
  AUTONOMOUS_FLOOR_TICK_MS,
  serializeLedger, restoreLedger, refreshSurfaceCatalog,
  EXAM_RECORDS_CAP, EXAM_NOTE_CAP, EXAM_LEDGER_MAX_BYTES,
  MAX_IMAGE_BASE64_LENGTH, MAX_IMAGE_DOWNLOAD_BYTES, MAX_IMAGES_PER_TURN, MAX_TEXT_ATTACHMENT_BYTES,
  modelContextLimit, compactionThreshold, buildCompactionCheckpoint,
  formatStrategicMemoryNotes, formatCheckpointState, resolveCheckpointGrounding,
  type CheckpointGrounding, type CheckpointStateResponse,
  decisionsRegisterText, serializeDecisionsReg, restoreDecisionsReg,
  isSettledDecisionVerdict, admitDecisionEntry,
  DECISIONS_REGISTER_CAP, DECISIONS_REG_MAX_BYTES,
  DEFAULT_CONTEXT_LIMIT, COMPACTION_THRESHOLD_RATIO,
  type ExamSurface, type ExamRecord, type ContentBlock,
  type CompactionCheckpointContext, type DecisionsRegisterEntry,
  type PendingOperatorDecisionReview,
} from './strategic-resident-lib.js';
import { buildFetchGuidance, STRATEGIC_FETCH_VERBS } from './strategic-helpers.js';

describe('residentConfig', () => {
  it('uses contract defaults when the payload omits the knobs', () => {
    const c = residentConfig({});
    expect(c.idleTickMs).toBe(5000);
    expect(c.messagePollMs).toBe(1500); // fast operator pickup default (1.5s)
    expect(c.turnTimeoutMs).toBe(120_000);
    expect(c.autonomousEnabled).toBe(true);
    expect(c.minTickMs).toBe(600_000);
    expect(c.autonomousTurnTimeoutMs).toBe(300_000);
    expect(c.maxOpsPerTurn).toBe(3);
    expect(c.compactionThresholdTokens).toBeUndefined(); // model-derived by default
    expect(c.compactionCooldownMs).toBe(300_000); // 5 min default
    expect(c.operatorAckThresholdMs).toBe(10_000); // 10s default
  });

  it('reads the payload knobs (seconds → ms)', () => {
    const c = residentConfig({
      strategic_session_idle_tick_seconds: 3,
      strategic_session_message_poll_seconds: 1,
      strategic_session_turn_timeout_seconds: 90,
      strategic_session_min_tick_seconds: 120,
      strategic_session_autonomous_turn_timeout_seconds: 180,
      strategic_session_max_ops_per_turn: 5,
      strategic_session_operator_ack_threshold_seconds: 30,
    });
    expect(c.idleTickMs).toBe(3000);
    expect(c.messagePollMs).toBe(1000);
    expect(c.turnTimeoutMs).toBe(90_000);
    expect(c.minTickMs).toBe(120_000);
    expect(c.autonomousTurnTimeoutMs).toBe(180_000);
    expect(c.maxOpsPerTurn).toBe(5);
    expect(c.operatorAckThresholdMs).toBe(30_000);
  });

  it('disables early ack when threshold is set to 0 or negative, and clamps to 1s floor', () => {
    expect(residentConfig({ strategic_session_operator_ack_threshold_seconds: 0 }).operatorAckThresholdMs).toBe(0);
    expect(residentConfig({ strategic_session_operator_ack_threshold_seconds: -5 }).operatorAckThresholdMs).toBe(0);
    // Small positive values clamp to the 1s floor
    expect(residentConfig({ strategic_session_operator_ack_threshold_seconds: 0.1 }).operatorAckThresholdMs).toBe(1000);
  });

  it('clamps to safe minimums', () => {
    const c = residentConfig({
      strategic_session_idle_tick_seconds: 0,
      strategic_session_message_poll_seconds: 0,
      strategic_session_turn_timeout_seconds: 1,
      strategic_session_autonomous_turn_timeout_seconds: 1,
      strategic_session_max_ops_per_turn: 0,
    });
    expect(c.idleTickMs).toBe(1000);
    // Floored at 750ms, but never slower than the idle tick (1000ms here).
    expect(c.messagePollMs).toBe(750);
    expect(c.turnTimeoutMs).toBe(15_000);
    expect(c.autonomousTurnTimeoutMs).toBe(15_000);
    expect(c.maxOpsPerTurn).toBe(1); // clamped to minimum 1
  });

  it('never polls slower than the idle heartbeat tick', () => {
    const c = residentConfig({
      strategic_session_idle_tick_seconds: 1,
      strategic_session_message_poll_seconds: 10,
    });
    expect(c.idleTickMs).toBe(1000);
    expect(c.messagePollMs).toBe(1000); // clamped down to the idle tick
  });

  it('minTickMs is never below the idle tick', () => {
    const c = residentConfig({
      strategic_session_idle_tick_seconds: 30,
      strategic_session_min_tick_seconds: 5,
    });
    // min_tick (5s) is below idle_tick (30s) — clamp to idle_tick
    expect(c.minTickMs).toBe(30_000);
  });

  it('disables autonomous work in monitor mode', () => {
    expect(residentConfig({ interaction_mode: 'monitor' }).autonomousEnabled).toBe(false);
    expect(residentConfig({ strategic_session_autonomous: false }).autonomousEnabled).toBe(false);
    expect(residentConfig({ interaction_mode: 'continuous' }).autonomousEnabled).toBe(true);
  });
});

describe('initialCursor', () => {
  it('prefers the session context message_cursor', () => {
    const cur = initialCursor({ context: { message_cursor: '2026-01-01T00:00:00Z' } }, {});
    expect(cur).toBe('2026-01-01T00:00:00Z');
  });

  it('falls back to the payload message_cursor', () => {
    const cur = initialCursor({}, { message_cursor: '2026-02-02T00:00:00Z' });
    expect(cur).toBe('2026-02-02T00:00:00Z');
  });

  it('defaults to a short window in the past', () => {
    const now = Date.UTC(2026, 5, 1, 12, 0, 0);
    const cur = initialCursor({}, {}, now);
    expect(new Date(cur).getTime()).toBe(now - 2 * 60_000);
  });
});

describe('advanceCursorFromMessages', () => {
  it('advances to the MAX created_at among returned messages (not server_time)', () => {
    const msgs = [
      { id: 'a', created_at: '2026-03-03T00:00:00Z' },
      { id: 'c', created_at: '2026-03-03T00:00:02Z' },
      { id: 'b', created_at: '2026-03-03T00:00:01Z' },
    ];
    expect(advanceCursorFromMessages(msgs, 'old')).toBe('2026-03-03T00:00:02Z');
  });

  it('keeps the prior cursor when no message carries a parseable created_at', () => {
    expect(advanceCursorFromMessages([], '2026-01-01T00:00:00Z')).toBe('2026-01-01T00:00:00Z');
    expect(advanceCursorFromMessages([{ id: 'x' }], 'keepme')).toBe('keepme');
    expect(advanceCursorFromMessages(null, 'keepme')).toBe('keepme');
  });

  it('never regresses below the current cursor', () => {
    const msgs = [{ id: 'old', created_at: '2020-01-01T00:00:00Z' }];
    expect(advanceCursorFromMessages(msgs, '2026-06-01T00:00:00Z')).toBe('2026-06-01T00:00:00Z');
  });

  it('orders numerically so mixed ISO precision cannot mis-advance', () => {
    // '...00.500Z' > '...01Z' lexically but is EARLIER in time — numeric compare wins.
    const msgs = [
      { id: 'a', created_at: '2026-03-03T00:00:00.500Z' },
      { id: 'b', created_at: '2026-03-03T00:00:01Z' },
    ];
    expect(advanceCursorFromMessages(msgs, 'old')).toBe('2026-03-03T00:00:01Z');
  });
});

describe('BoundedSeenSet / dedupNewMessages', () => {
  it('drops ids already seen and records new ones', () => {
    const seen = new BoundedSeenSet();
    const page1 = dedupNewMessages([{ id: 'a' }, { id: 'b' }], seen);
    expect(page1.map(m => m.id)).toEqual(['a', 'b']);
    // Re-returning 'b' at the >= boundary plus a genuinely new 'c'.
    const page2 = dedupNewMessages([{ id: 'b' }, { id: 'c' }], seen);
    expect(page2.map(m => m.id)).toEqual(['c']);
  });

  it('passes through messages with no id (cannot dedup) but dedups those with ids', () => {
    const seen = new BoundedSeenSet();
    const out = dedupNewMessages([{ content: 'no-id' }, { id: 'a' }], seen);
    expect(out).toHaveLength(2);
    const again = dedupNewMessages([{ id: 'a' }], seen);
    expect(again).toHaveLength(0);
  });

  it('evicts oldest ids past the cap but keeps recent ones deduped', () => {
    const seen = new BoundedSeenSet(2);
    seen.add('a'); seen.add('b'); seen.add('c'); // 'a' evicted (cap 2)
    expect(seen.has('a')).toBe(false);
    expect(seen.has('b')).toBe(true);
    expect(seen.has('c')).toBe(true);
    expect(seen.size).toBe(2);
  });
});

describe('resolveTerminalStatus', () => {
  it('COMPLETED on a clean operator/task-driven stop', () => {
    expect(resolveTerminalStatus('stop_requested', null)).toEqual({ status: 'COMPLETED' });
    expect(resolveTerminalStatus('task_terminal', 1)).toEqual({ status: 'COMPLETED' });
  });

  it('FAILED (claude_code_failed) on a non-zero or signal-killed process exit', () => {
    expect(resolveTerminalStatus('session_process_exited', 1))
      .toEqual({ status: 'FAILED', failureCategory: 'claude_code_failed' });
    expect(resolveTerminalStatus('session_process_exited', null))
      .toEqual({ status: 'FAILED', failureCategory: 'claude_code_failed' });
  });

  it('COMPLETED on an orderly exit(0)', () => {
    expect(resolveTerminalStatus('session_process_exited', 0)).toEqual({ status: 'COMPLETED' });
  });

  it('FAILED (timeout) on a hard timeout / stalled session', () => {
    expect(resolveTerminalStatus('turn_hard_timeout', null))
      .toEqual({ status: 'FAILED', failureCategory: 'timeout' });
    expect(resolveTerminalStatus('session_stalled', null))
      .toEqual({ status: 'FAILED', failureCategory: 'timeout' });
  });

  it('FAILED (claude_code_failed) on a loop error', () => {
    expect(resolveTerminalStatus('loop_error: boom', null))
      .toEqual({ status: 'FAILED', failureCategory: 'claude_code_failed' });
  });
});

describe('MESSAGE_POLL_LIMIT', () => {
  it('matches the contract page size', () => {
    expect(MESSAGE_POLL_LIMIT).toBe(20);
  });
});

describe('extractMessageTurn / combineOperatorMessages', () => {
  it('extracts and trims content as a text block', () => {
    const r1 = extractMessageTurn({ content: '  hello  ' });
    expect(r1).toHaveLength(1);
    expect(r1[0]).toEqual({ type: 'text', text: 'hello' });

    const r2 = extractMessageTurn({});
    expect(r2).toHaveLength(0);
  });

  it('extracts inline base64 image attachments from context', () => {
    const r = extractMessageTurn({
      content: 'see this',
      context: {
        attachments: [
          { type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=' },
        ],
      },
    });
    expect(r).toHaveLength(2);
    expect(r[0]).toEqual({ type: 'text', text: 'see this' });
    expect(r[1]).toEqual({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } });
  });

  it('returns a single message verbatim', () => {
    const out = combineOperatorMessages([{ content: 'just one' }]);
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({ type: 'text', text: 'just one' });
  });

  it('batches multiple messages with headers', () => {
    const out = combineOperatorMessages([{ content: 'first' }, { content: 'second' }]);
    expect(out.length).toBeGreaterThanOrEqual(2);
    const texts = out.filter((b: ContentBlock): b is { type: 'text'; text: string } => b.type === 'text').map(b => b.text);
    expect(texts.some(t => t.includes('first'))).toBe(true);
    expect(texts.some(t => t.includes('second'))).toBe(true);
  });

  it('skips empty messages', () => {
    expect(combineOperatorMessages([{ content: '' }, { content: '   ' }])).toHaveLength(0);
  });

  it('preserves image blocks through combine', () => {
    const out = combineOperatorMessages([
      { content: 'check', context: { attachments: [{ type: 'image', mime_type: 'image/jpeg', data: '/9j/4AAQ=' }] } },
    ]);
    expect(out).toHaveLength(2);
    expect(out[0]).toEqual({ type: 'text', text: 'check' });
    expect(out[1]).toHaveProperty('type', 'image');
    expect((out[1] as unknown as { source: Record<string, unknown> }).source).toEqual({ type: 'base64', media_type: 'image/jpeg', data: '/9j/4AAQ=' });
  });

  it('preserves image blocks through combine batch path (2+ messages)', () => {
    const out = combineOperatorMessages([
      { content: 'first', context: { attachments: [{ type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=' }] } },
      { content: 'second with attachment', context: { attachments: [{ type: 'image', mime_type: 'image/jpeg', data: '/9j/4AAQ=' }] } },
    ]);
    // First message: header + text + image (3 blocks).
    // Separator + second header + second text + second image (4 more blocks).
    expect(out.length).toBeGreaterThanOrEqual(6);
    const texts = out.filter((b: ContentBlock) => b.type === 'text').map(b => b.text);
    expect(texts.some(t => t.includes('first'))).toBe(true);
    expect(texts.some(t => t.includes('second with attachment'))).toBe(true);
    const images = out.filter((b: ContentBlock) => b.type === 'image');
    expect(images).toHaveLength(2);
    expect(images[0]).toHaveProperty('source.data', 'iVBORw0KGgo=');
    expect(images[1]).toHaveProperty('source.data', '/9j/4AAQ=');
  });

  it('rejects non-image and unsupported image mime_types in extractMessageTurn', () => {
    const r = extractMessageTurn({
      content: 'file attached',
      context: {
        attachments: [
          { type: 'image', mime_type: 'application/pdf', data: 'JVBERi0=' },
          { type: 'image', mime_type: 'image/svg+xml', data: 'PHN2Zz4=' },
          { type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=' },
        ],
      },
    });
    // Only the valid image/png block should be included.
    expect(r).toHaveLength(2);
    expect(r[0]).toEqual({ type: 'text', text: 'file attached' });
    expect(r[1]).toEqual({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } });
  });

  it('drops oversized image base64 data in extractMessageTurn', () => {
    const oversized = 'x'.repeat(MAX_IMAGE_BASE64_LENGTH + 1);
    const r = extractMessageTurn({
      content: 'big image',
      context: {
        attachments: [
          { type: 'image', mime_type: 'image/png', data: oversized },
          { type: 'image', mime_type: 'image/jpeg', data: '/9j/4AAQ=' },
        ],
      },
    });
    // Only the second (valid-size) image should be extracted alongside the text.
    expect(r).toHaveLength(2);
    expect(r[0]).toEqual({ type: 'text', text: 'big image' });
    expect(r[1]).toHaveProperty('type', 'image');
    expect(r[1]).toHaveProperty('source.data', '/9j/4AAQ=');
  });

  it('extractMessageTurn delivers an operator image produced by the real frontend attachment shape as an image block', () => {
    // The frontend's fileToMessageAttachment sets type:'image' for image/* mime types.
    // This test verifies that extractMessageTurn accepts that shape and emits an image block.
    const r = extractMessageTurn({
      content: 'see screenshot',
      context: {
        attachments: [
          { type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=', name: 'screenshot.png', size_bytes: 9 },
        ],
      },
    });
    expect(r).toHaveLength(2);
    expect(r[0]).toEqual({ type: 'text', text: 'see screenshot' });
    expect(r[1]).toEqual({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } });
  });

  it('extractMessageTurn emits decoded text for a text/markdown attachment and a reference block for a PDF', () => {
    const markdownContent = '# Hello\n\nThis is a markdown file.';
    const markdownBase64 = Buffer.from(markdownContent).toString('base64');
    const r = extractMessageTurn({
      content: 'check these files',
      context: {
        attachments: [
          { type: 'file', mime_type: 'text/markdown', data: markdownBase64, name: 'README.md', size_bytes: markdownContent.length },
          { type: 'file', mime_type: 'application/pdf', data: 'JVBERi0=', name: 'document.pdf', size_bytes: 6 },
        ],
      },
    });
    // text block + decoded markdown + PDF reference
    expect(r).toHaveLength(3);
    expect(r[0]).toEqual({ type: 'text', text: 'check these files' });
    // Decoded text/markdown attachment
    expect(r[1].type).toBe('text');
    const markdownBlock = r[1] as { type: 'text'; text: string };
    expect(markdownBlock.text).toContain('README.md');
    expect(markdownBlock.text).toContain(markdownContent);
    // PDF reference block (no decoding — opaque binary)
    expect(r[2].type).toBe('text');
    const pdfBlock = r[2] as { type: 'text'; text: string };
    expect(pdfBlock.text).toContain('document.pdf');
    expect(pdfBlock.text).toContain('application/pdf');
    expect(pdfBlock.text).toContain('6 bytes');
  });

  it('caps the number of image attachments per turn', () => {
    const attachments: Array<Record<string, string>> = Array.from({ length: MAX_IMAGES_PER_TURN + 3 }, (_, i) => ({
      type: 'image',
      mime_type: 'image/png',
      data: `data${i}`,
    }));
    const r = extractMessageTurn({
      content: 'many images',
      context: { attachments },
    });
    // One text block + MAX_IMAGES_PER_TURN image blocks.
    expect(r).toHaveLength(1 + MAX_IMAGES_PER_TURN);
    expect(r[0]).toEqual({ type: 'text', text: 'many images' });
    // All extracted image blocks should have distinct data values.
    expect(r.filter(b => b.type === 'image')).toHaveLength(MAX_IMAGES_PER_TURN);
    for (let i = 0; i < MAX_IMAGES_PER_TURN; i++) {
      expect(r[i + 1]).toHaveProperty('source.data', `data${i}`);
    }
  });

  it('extracts GCS URL-sourced image attachment from context', () => {
    const signedUrl = 'https://storage.googleapis.com/bucket/screenshot.png?X-Goog-Signature=abc123';
    const r = extractMessageTurn({
      content: 'see the screenshot',
      context: {
        attachments: [
          {
            type: 'image',
            mime_type: 'image/png',
            url: signedUrl,
            name: 'screenshot.png',
          },
        ],
      },
    });
    expect(r).toHaveLength(2);
    expect(r[0]).toEqual({ type: 'text', text: 'see the screenshot' });
    expect(r[1]).toEqual({
      type: 'image',
      source: {
        type: 'url',
        url: signedUrl,
        media_type: 'image/png',
        name: 'screenshot.png',
      },
    });
  });

  it('rejects unsigned or non-GCS image attachment URLs before fetch', () => {
    expect(isAllowedAttachmentDownloadUrl('https://storage.googleapis.com/bucket/a.png?X-Goog-Signature=abc123')).toBe(true);
    expect(isAllowedAttachmentDownloadUrl('https://bucket.storage.googleapis.com/a.png?X-Goog-Signature=abc123')).toBe(true);
    expect(isAllowedAttachmentDownloadUrl('https://storage.googleapis.com/bucket/a.png')).toBe(false);
    expect(isAllowedAttachmentDownloadUrl('http://storage.googleapis.com/bucket/a.png?X-Goog-Signature=abc123')).toBe(false);
    expect(isAllowedAttachmentDownloadUrl('https://example.com/a.png?X-Goog-Signature=abc123')).toBe(false);

    const r = extractMessageTurn({
      content: 'bad url',
      context: {
        attachments: [
          { type: 'image', mime_type: 'image/png', url: 'https://example.com/a.png?X-Goog-Signature=abc123', name: 'a.png' },
        ],
      },
    });
    expect(r).toEqual([
      { type: 'text', text: 'bad url' },
      { type: 'text', text: '[Attached image "a.png" could not be loaded: unsupported download URL.]' },
    ]);
  });

  it('prefers base64 data over GCS URL when both are present', () => {
    const r = extractMessageTurn({
      content: 'inline data',
      context: {
        attachments: [
          {
            type: 'image',
            mime_type: 'image/jpeg',
            data: '/9j/4AAQ=',
            url: 'https://storage.googleapis.com/bucket/photo.jpg?signed=...',
          },
        ],
      },
    });
    // Should use the base64 data, not the URL.
    expect(r).toHaveLength(2);
    expect(r[1]).toHaveProperty('type', 'image');
    expect(r[1]).toHaveProperty('source.type', 'base64');
    expect(r[1]).toHaveProperty('source.data', '/9j/4AAQ=');
  });

  it('materializes signed GCS image URLs as base64 model blocks', async () => {
    const signedUrl = 'https://storage.googleapis.com/bucket/photo.png?X-Goog-Signature=abc123';
    const bytes = new Uint8Array([1, 2, 3, 4]);
    const fetchCalls: string[] = [];
    const out = await materializeContentBlocks([
      { type: 'text', text: 'look' },
      { type: 'image', source: { type: 'url', url: signedUrl, media_type: 'image/png', name: 'photo.png' } },
    ], (url) => {
      fetchCalls.push(url);
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: {
          get(name: string) {
            if (name.toLowerCase() === 'content-length') return String(bytes.byteLength);
            if (name.toLowerCase() === 'content-type') return 'image/png';
            return null;
          },
        },
        arrayBuffer: () => Promise.resolve(bytes.buffer),
      });
    });

    expect(fetchCalls).toEqual([signedUrl]);
    expect(out).toEqual([
      { type: 'text', text: 'look' },
      {
        type: 'image',
        source: {
          type: 'base64',
          media_type: 'image/png',
          data: Buffer.from(bytes).toString('base64'),
        },
      },
    ]);
  });

  it('materializes URL fetch failures as text notices without throwing', async () => {
    const out = await materializeContentBlocks([
      {
        type: 'image',
        source: {
          type: 'url',
          url: 'https://storage.googleapis.com/bucket/missing.png?X-Goog-Signature=abc123',
          media_type: 'image/png',
          name: 'missing.png',
        },
      },
    ], () => Promise.resolve({
      ok: false,
      status: 403,
      headers: { get: () => null },
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    }));

    expect(out).toEqual([
      { type: 'text', text: '[Attached image "missing.png" could not be loaded: download failed (403).]' },
    ]);
  });

  it('does not download images declared larger than the model input cap', async () => {
    let readBody = false;
    const out = await materializeContentBlocks([
      {
        type: 'image',
        source: {
          type: 'url',
          url: 'https://storage.googleapis.com/bucket/huge.png?X-Goog-Signature=abc123',
          media_type: 'image/png',
          name: 'huge.png',
        },
      },
    ], () => Promise.resolve({
      ok: true,
      status: 200,
      headers: {
        get(name: string) {
          return name.toLowerCase() === 'content-length'
            ? String(MAX_IMAGE_DOWNLOAD_BYTES + 1)
            : null;
        },
      },
      arrayBuffer: () => {
        readBody = true;
        return Promise.resolve(new ArrayBuffer(0));
      },
    }));

    expect(readBody).toBe(false);
    expect(out).toEqual([
      { type: 'text', text: '[Attached image "huge.png" could not be loaded: image is too large.]' },
    ]);
  });

  it('text attachment content reaches the model via GCS URL fetch', async () => {
    // Operator uploads a Python file; backend stores only {type, url, mime_type, name} — no inline data.
    const signedUrl = 'https://storage.googleapis.com/bucket/script.py?X-Goog-Signature=abc123';
    const fileContent = 'def hello():\n    print("Hello, world!")\n';
    const fetchCalls: string[] = [];

    // Step 1: extractMessageTurn emits a text_url block for the URL-only text file.
    const blocks = extractMessageTurn({
      content: 'review this file',
      context: {
        attachments: [
          { type: 'file', mime_type: 'text/x-python', url: signedUrl, name: 'script.py', size_bytes: fileContent.length },
        ],
      },
    });
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toEqual({ type: 'text', text: 'review this file' });
    expect(blocks[1]).toEqual({ type: 'text_url', url: signedUrl, name: 'script.py', mime_type: 'text/x-python' });

    // Step 2: materializeContentBlocks fetches the URL and inlines the content as a text block.
    const out = await materializeContentBlocks(blocks, (url) => {
      fetchCalls.push(url);
      const encoder = new TextEncoder();
      const bytes = encoder.encode(fileContent);
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        arrayBuffer: () => Promise.resolve(bytes.buffer),
      });
    });

    expect(fetchCalls).toEqual([signedUrl]);
    expect(out).toHaveLength(2);
    expect(out[0]).toEqual({ type: 'text', text: 'review this file' });
    expect(out[1]).toEqual({ type: 'text', text: `[Attachment: script.py]\n${fileContent}` });
    // Verify the file body is present — this is the seam: content reaches the model input.
    expect(out[1].type === 'text' && out[1].text).toContain('def hello()');
  });

  it('text attachment URL fetch failure degrades to a notice without throwing', async () => {
    const signedUrl = 'https://storage.googleapis.com/bucket/notes.txt?X-Goog-Signature=abc123';
    const blocks = extractMessageTurn({
      content: 'here',
      context: {
        attachments: [{ type: 'file', mime_type: 'text/plain', url: signedUrl, name: 'notes.txt', size_bytes: 100 }],
      },
    });
    const out = await materializeContentBlocks(blocks, () => Promise.resolve({
      ok: false,
      status: 403,
      headers: { get: () => null },
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    }));
    expect(out).toEqual([
      { type: 'text', text: 'here' },
      { type: 'text', text: '[Attachment "notes.txt" could not be loaded: download failed (403).]' },
    ]);
  });

  it('text attachment content is truncated when it exceeds MAX_TEXT_ATTACHMENT_BYTES', async () => {
    const signedUrl = 'https://storage.googleapis.com/bucket/big.txt?X-Goog-Signature=abc123';
    const blocks = extractMessageTurn({
      content: 'check this',
      context: {
        attachments: [{ type: 'file', mime_type: 'text/plain', url: signedUrl, name: 'big.txt', size_bytes: MAX_TEXT_ATTACHMENT_BYTES + 10 }],
      },
    });
    const bigContent = 'A'.repeat(MAX_TEXT_ATTACHMENT_BYTES + 10);
    const encoder = new TextEncoder();
    const bytes = encoder.encode(bigContent);
    const out = await materializeContentBlocks(blocks, () => Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => null },
      arrayBuffer: () => Promise.resolve(bytes.buffer),
    }));
    expect(out).toHaveLength(2);
    const textOut = out[1].type === 'text' ? out[1].text : '';
    expect(textOut).toContain('[... truncated]');
    // Output should not contain the full original content (only the sliced prefix).
    expect(textOut).not.toContain(bigContent);
  });
});

describe('buildBootstrapPrompt', () => {
  const base = {
    contextPrefix: 'CTX',
    projectName: 'Acme',
    problemModelJson: '{"gaps":[]}',
    runningUpdate: 'all good',
    directionsText: '- d1 active',
    stagedDirectionsText: '',
    retiredDirectionsText: '',
    previousDecisionOutcomesText: '',
    visionJson: '{"v":1}',
    openMessagesText: 'msg1',
    sessionId: 'ss-1',
    fetchGuidanceText: '',
  };

  it('includes the role framing, tools, and grounding sections', () => {
    const p = buildBootstrapPrompt(base);
    expect(p).toContain('resident strategist for Acme');
    expect(p).toContain('./strategic');
    expect(p).toContain('Current problem model');
    expect(p).toContain('{"gaps":[]}');
    expect(p).toContain('all good');
    expect(p).toContain('- d1 active');
    expect(p).toContain('CTX');
  });

  it('bootstrap_prompt_uses_quote_safe_message_delivery', () => {
    const p = buildBootstrapPrompt(base);
    expect(p).toContain("./messages send '<answer>'");
    expect(p).toContain('./messages send --stdin');
    expect(p).toContain('apostrophe-heavy or multiline replies');
    expect(p).toContain('heredoc or pipe');
  });

  it('bootstrap_prompt_states_dispatched_work_runs_concurrently', () => {
    const p = buildBootstrapPrompt(base);
    expect(p).toContain('Dispatched work runs concurrently and independently');
    expect(p).toContain('dispatching BOTH now is the DEFAULT');
    expect(p).toContain('do not stage one behind the other');
    expect(p).toContain('conflicts_with');
  });

  it('includes canonical fetch guidance when provided', () => {
    const p = buildBootstrapPrompt({
      ...base,
      fetchGuidanceText: buildFetchGuidance(STRATEGIC_FETCH_VERBS),
    });
    expect(p).toContain('MID-RUN FETCH (./fetch)');
    expect(p).toContain('`./fetch task <task_id>`');
    expect(p).toContain('`./fetch tasks`');
  });

  it('renders an empty-problem-model note when none is present', () => {
    const p = buildBootstrapPrompt({ ...base, problemModelJson: '' });
    expect(p).toContain('empty — bootstrap it');
  });

  it('includes the optional retired and prior-decision sections when provided', () => {
    const p = buildBootstrapPrompt({
      ...base,
      retiredDirectionsText: 'retired block',
      previousDecisionOutcomesText: 'prior block',
    });
    expect(p).toContain('Recently retired / completed directions');
    expect(p).toContain('retired block');
    expect(p).toContain('Prior-cycle decision outcomes');
    expect(p).toContain('prior block');
  });

  it('omits optional sections that are blank', () => {
    const p = buildBootstrapPrompt({
      ...base, runningUpdate: '', directionsText: '', visionJson: '', openMessagesText: '',
    });
    expect(p).not.toContain('Active directions');
    expect(p).not.toContain('Open operator messages');
    expect(p).not.toContain('## Vision');
    expect(p).not.toContain('Recently retired / completed directions');
    expect(p).not.toContain('Prior-cycle decision outcomes');
    expect(p).not.toContain('Already-staged (next-version) directions');
  });

  it('renders the recently-answered operator conversation as a read-only section', () => {
    const p = buildBootstrapPrompt({
      ...base,
      recentHandledConversationText: '- [FEEDBACK] (id: h1, from: user, answered) drop the caching idea',
    });
    expect(p).toContain('Recently answered operator conversation');
    expect(p).toContain('read-only — already handled, do NOT re-answer');
    expect(p).toContain('drop the caching idea');
    expect(p).toContain('do not re-propose it');
  });

  it('omits the recently-answered conversation section when blank/absent', () => {
    expect(buildBootstrapPrompt(base)).not.toContain('Recently answered operator conversation');
    expect(
      buildBootstrapPrompt({ ...base, recentHandledConversationText: '   ' }),
    ).not.toContain('Recently answered operator conversation');
  });

  it('includes standing behavioral lessons when memory notes are provided', () => {
    const p = buildBootstrapPrompt({
      ...base,
      strategicMemoryNotesText: '- [2026-07-01] Prefer concrete action over abstract discussion\n- [2026-07-02] Verify claims before escalating',
    });
    expect(p).toContain('## Standing behavioral lessons');
    expect(p).toContain('Accumulated across sessions');
    expect(p).toContain('./strategic set-memory');
    expect(p).toContain('- [2026-07-01] Prefer concrete action over abstract discussion');
    expect(p).toContain('- [2026-07-02] Verify claims before escalating');
  });

  it('omits standing behavioral lessons when memory notes is empty', () => {
    const p = buildBootstrapPrompt({ ...base, strategicMemoryNotesText: '' });
    expect(p).not.toContain('## Standing behavioral lessons');
    expect(p).not.toContain('Accumulated across sessions');
  });

  it('includes the project tool content section when provided', () => {
    const p = buildBootstrapPrompt({
      ...base,
      toolContentText: '### Notes (notes)\n# Idea\n\nShip the retry path.',
    });
    expect(p).toContain('## Project Tool Content');
    expect(p).toContain('### Notes (notes)');
    expect(p).toContain('Ship the retry path.');
    expect(p).toContain('./fetch tools');
  });

  it('omits the project tool content section when empty', () => {
    const p = buildBootstrapPrompt({ ...base, toolContentText: '' });
    expect(p).not.toContain('## Project Tool Content');
  });

  it('omits standing behavioral lessons when memory notes is undefined', () => {
    const p = buildBootstrapPrompt(base); // strategicMemoryNotesText not set
    expect(p).not.toContain('## Standing behavioral lessons');
    expect(p).not.toContain('Accumulated across sessions');
  });

  it('includes prior session context when priorSessionContextText is provided', () => {
    const p = buildBootstrapPrompt({
      ...base,
      priorSessionContextText: 'Previous session explored authentication flows.',
    });
    expect(p).toContain('## Prior session context');
    expect(p).toContain('Previous session explored authentication flows.');
  });

  it('omits prior session context when priorSessionContextText is empty', () => {
    const p = buildBootstrapPrompt({ ...base, priorSessionContextText: '' });
    expect(p).not.toContain('## Prior session context');
  });

  it('omits prior session context when priorSessionContextText is undefined', () => {
    const p = buildBootstrapPrompt(base); // priorSessionContextText not set
    expect(p).not.toContain('## Prior session context');
  });

  it('decisions_register_reaches_bootstrap_on_full_restart', () => {
    // On a full session restart the durable decisions register is restored from
    // the persisted context and rendered by decisionsRegisterText, then threaded
    // into the bootstrap prompt so the first (usually operator-reply) turn is not
    // blind to settled decisions. This test exercises that seam end-to-end.
    const register: DecisionsRegisterEntry[] = [
      { at: '2026-07-10T00:00:00Z', atTick: 3, verdict: 'SETTLED', surfaceKey: 'direction:d-a5940d77', note: 'attestation tiering approved by operator' },
      { at: '2026-07-11T00:00:00Z', atTick: 7, verdict: 'REJECTED', surfaceKey: 'direction:d-68d4ef5a', note: 'operator declined project-baked image rollout' },
    ];
    const p = buildBootstrapPrompt({
      ...base,
      decisionsRegisterText: decisionsRegisterText(register, DECISIONS_REGISTER_CAP),
    });
    expect(p).toContain('## Decisions register (durable cross-tick memory)');
    expect(p).toContain('direction:d-a5940d77');
    expect(p).toContain('attestation tiering approved by operator');
    expect(p).toContain('operator declined project-baked image rollout');
  });

  it('omits the decisions register on a clean restart (empty register)', () => {
    // A clean restart restores an empty register; decisionsRegisterText([]) === ''
    // so the section must be omitted (byte-for-byte no-op against the base prompt).
    const withEmpty = buildBootstrapPrompt({
      ...base,
      decisionsRegisterText: decisionsRegisterText([], DECISIONS_REGISTER_CAP),
    });
    expect(withEmpty).not.toContain('## Decisions register');
    // Omitted field behaves identically to an empty string.
    expect(buildBootstrapPrompt(base)).not.toContain('## Decisions register');
    expect(withEmpty).toEqual(buildBootstrapPrompt(base));
  });
});

describe('retired/prior-context renderers', () => {
  it('renders compact staged-direction lines and omits empty input', () => {
    expect(buildStagedDirectionsText([])).toBe('');
    expect(buildStagedDirectionsText(undefined)).toBe('');
    const rendered = buildStagedDirectionsText([
      {
        id: 'd-stage',
        text: 'Stage this work',
        priority: 'high',
        staged_for_version: 12,
        staged_at: '2026-06-01T12:00:00Z',
        staged_age_days: 30,
      },
    ]);
    expect(rendered).not.toContain('## Already-staged (next-version) directions');
    expect(rendered).toContain('Stage this work (id: d-stage)');
    expect(rendered).toContain('priority=high');
    expect(rendered).toContain('staged_for_version=12');
    expect(rendered).toContain('staged_at=2026-06-01T12:00:00Z');
    expect(rendered).toContain('staged_age_days=30');
  });

  it('renders compact retired-direction lines and omits empty input', () => {
    expect(buildRetiredContextText([])).toBe('');
    expect(buildRetiredContextText(undefined)).toBe('');
    const rendered = buildRetiredContextText([
      {
        id: 'd-ret',
        text: 'Retired direction',
        status: 'retired',
        cycles: 4,
        prs_merged: 7,
        retirement_reason: 'merged elsewhere',
        retirement_outcome: 'landed on integration',
      },
    ]);
    expect(rendered).not.toContain('## Recently retired / completed directions');
    expect(rendered).toContain('[retired] Retired direction (id: d-ret)');
    expect(rendered).toContain('cycles=4');
    expect(rendered).toContain('prs_merged=7');
    expect(rendered).toContain('reason=merged elsewhere');
    expect(rendered).toContain('outcome=landed on integration');
  });

  it('renders compact prior-decision lines and omits empty input', () => {
    expect(buildPriorDecisionsText(undefined)).toBe('');
    expect(buildPriorDecisionsText({ created: [], retired: [] })).toBe('');
    const rendered = buildPriorDecisionsText({
      cycle_id: 'sc-123',
      summary_text: 'rev 5, 2 created, 1 retired',
      cycle_reasoning: 'calibrated on shipped work',
      created: [
        {
          direction_id: 'd-new',
          status: 'active',
          prs_merged: 3,
          rationale: 'cover the missing API surface',
          calibration_delta: 1,
        },
      ],
      retired: [
        {
          direction_id: 'd-old',
          status: 'retired',
          prs_merged: 8,
        },
      ],
    });
    expect(rendered).not.toContain('## Prior-cycle decision outcomes');
    expect(rendered).toContain('cycle=sc-123');
    expect(rendered).toContain('Created directions:');
    expect(rendered).toContain('created d-new');
    expect(rendered).toContain('status=active');
    expect(rendered).toContain('prs_merged=3');
    expect(rendered).toContain('rationale=cover the missing API surface');
    expect(rendered).toContain('calibration_delta=1');
    expect(rendered).toContain('Retired directions:');
    expect(rendered).toContain('retired d-old');
  });

  it('headers appear exactly once when renderers are composed into buildBootstrapPrompt', () => {
    const retiredText = buildRetiredContextText([
      { id: 'd-r', text: 'Done work', status: 'retired', cycles: 1, prs_merged: 2 },
    ]);
    const priorText = buildPriorDecisionsText({
      cycle_id: 'sc-1',
      created: [{ direction_id: 'd-new', status: 'active', prs_merged: 0 }],
      retired: [],
    });
    const p = buildBootstrapPrompt({
      contextPrefix: 'CTX',
      projectName: 'Acme',
      problemModelJson: '{}',
      runningUpdate: 'ok',
      directionsText: '- d1',
      stagedDirectionsText: buildStagedDirectionsText([
        { id: 'd-stage', text: 'Stage this work', priority: 'medium', staged_for_version: 7 },
      ]),
      retiredDirectionsText: retiredText,
      previousDecisionOutcomesText: priorText,
      visionJson: '',
      openMessagesText: '',
      fetchGuidanceText: '',
      sessionId: 'ss-x',
    });
    const retiredMatches = (p.match(/## Recently retired \/ completed directions/g) ?? []).length;
    const priorMatches = (p.match(/## Prior-cycle decision outcomes/g) ?? []).length;
    const stagedMatches = (p.match(/## Already-staged \(next-version\) directions/g) ?? []).length;
    expect(retiredMatches).toBe(1);
    expect(priorMatches).toBe(1);
    expect(stagedMatches).toBe(1);
  });
});

describe('buildAutonomousPrompt (dynamic, evidence-gated)', () => {
  const focus: ExamSurface[] = [
    { key: 'direction:d-1', kind: 'direction', label: 'direction d-1', hint: './fetch direction d-1' },
    { key: 'code:backend', kind: 'code', label: 'code/backend', hint: 'read a module under backend/' },
  ];

  it('test_resident_prompt_states_running_update_file_path', () => {
    const p = buildAutonomousPrompt(focus, [], [], false);
    // set-running-update requires a file path, not inline markdown
    expect(p).toContain('set-running-update');
    expect(p).toContain('FILE PATH');
    expect(p).toContain('/tmp/running_update.md');
    expect(p).toContain('reads the file');
    expect(p).toContain('passing markdown as argv fails');
  });

  it('renders the work-unit contract and the TICK-VERDICT vocabulary', () => {
    const p = buildAutonomousPrompt(focus, [], [], false);
    // Work-unit contract, not "ONE small step".
    expect(p).toContain('ONE meaningful unit of progress');
    expect(p).not.toContain('ONE small');
    // All three verdicts are in the contract; the old global escape is gone.
    expect(p).toContain('TICK-VERDICT: ADVANCED');
    expect(p).toContain('TICK-VERDICT: FOUND');
    expect(p).toContain('TICK-VERDICT: CLEAR');
    expect(p).toContain('there is no global');
    // Rotating focus surfaces are listed.
    expect(p).toContain('direction:d-1');
    expect(p).toContain('code:backend');
    // Delta signals can never justify CLEAR.
    expect(p).toContain('can NEVER justify a CLEAR');
    // No unsolicited durable operator messages; status → set-running-update.
    expect(p).toContain('do NOT send unsolicited durable messages');
    expect(p).toContain('set-running-update');
    // Standing agenda continuation marker.
    expect(p).toContain('AGENDA-CONTINUE');
  });

  it('opens with the corrective examine-before-CLEAR instruction when flagged', () => {
    const p = buildAutonomousPrompt(focus, [], [], true);
    expect(p).toContain('CORRECTION');
    expect(p).toContain('examined nothing');
    expect(p).toContain('direction:d-1'); // the stalest surface to examine
  });

  it('restates recent records and open FOUND leads', () => {
    const recent: ExamRecord[] = [
      { surfaceKey: 'qualification', atTick: 1, at: 't', verdict: 'clear', note: 'no actionable findings' },
    ];
    const found: ExamRecord[] = [
      { surfaceKey: 'code:backend', atTick: 2, at: 't', verdict: 'found', note: 'hardcoded model id' },
    ];
    const p = buildAutonomousPrompt(focus, recent, found, false);
    expect(p).toContain('Recently examined');
    expect(p).toContain('qualification [clear]');
    expect(p).toContain('Open leads');
    expect(p).toContain('code:backend: hardcoded model id');
  });

  it('includes pipeline-parallelism reminder: dispatched work runs concurrently', () => {
    const p = buildAutonomousPrompt(focus, [], [], false);
    // Reminder that dispatched work runs concurrently and independent work should be dispatched together
    expect(p).toContain('Dispatched work runs concurrently');
    expect(p).toContain('spawn_task, spawn_fix, or');
    expect(p).toContain('pipeline regardless of your cadence');
    expect(p).toContain('genuinely independent');
    expect(p).toContain('dispatch BOTH now');
    expect(p).toContain('staging one behind the other');
  });
});

describe('decisionsRegisterText decisions register', () => {
  it('test_decisions_register_rendered_in_prompt_and_checkpoint', () => {
    const register: DecisionsRegisterEntry[] = [
      { atTick: 1, at: '2026-07-10T12:00:00Z', surfaceKey: 'direction:d-1', verdict: 'found', note: 'Found high churn in recent activity - needs review' },
      { atTick: 2, at: '2026-07-10T12:05:00Z', surfaceKey: 'direction:d-1', verdict: 'advanced', note: 'Spawned plan task to investigate churn root cause' },
      { atTick: 3, at: '2026-07-10T12:10:00Z', surfaceKey: 'qualification', verdict: 'clear', note: 'Examined latest qualification findings - nothing new actionable' },
    ];

    // 1. decisionsRegisterText formats entries with full-fidelity notes.
    const text = decisionsRegisterText(register, 20);
    expect(text).toContain('Decisions register');
    expect(text).toContain('tick-1 [found] direction:d-1');
    expect(text).toContain('Found high churn');
    expect(text).toContain('tick-2 [advanced] direction:d-1');
    expect(text).toContain('Spawned plan task');
    expect(text).toContain('tick-3 [clear] qualification');
    expect(text).toContain('Examined latest qualification findings');

    // 2. Decisions register renders into the autonomous prompt.
    const prompt = buildAutonomousPrompt([], [], [], false, 3, text);
    expect(prompt).toContain('Decisions register');
    expect(prompt).toContain('tick-1 [found]');

    // 3. Decisions register renders into the compaction checkpoint.
    const checkpoint = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      decisionsRegisterText: text,
    });
    expect(checkpoint).toContain('Decisions register');
    expect(checkpoint).toContain('tick-1 [found]');

    // 4. Empty register returns empty string.
    expect(decisionsRegisterText([], 20)).toBe('');

    // 5. Checkpoint with no decisions register omits the section.
    const noReg = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
    });
    expect(noReg).not.toContain('Decisions register');
  });

  it('includes the explicit operator-decision review protocol when a review is pending', () => {
    const focus: ExamSurface[] = [
      { key: 'direction:d-1', kind: 'direction', label: 'direction d-1', hint: 'check it' },
    ];
    const p = buildAutonomousPrompt(focus, [], [], false, 3, '', true);
    expect(p).toContain('OPERATOR-DECISION REVIEW');
    expect(p).toContain('OPERATOR-DECISION-REVIEW: NO-DECISION');
    expect(p).toContain('OPERATOR-DECISION-REVIEW: UNRESOLVED');
  });

  it('omits OPERATOR-DECISION CORRECTIVE section when operatorDecisionMissing is false (default)', () => {
    const focus: ExamSurface[] = [
      { key: 'direction:d-1', kind: 'direction', label: 'direction d-1', hint: 'check it' },
    ];
    const p = buildAutonomousPrompt(focus, [], [], false);
    expect(p).not.toContain('OPERATOR-DECISION CORRECTIVE');
  });
});

describe('isSettledDecisionVerdict (durable register admission)', () => {
  it('accepts only settled decision verdicts (advanced/found/clear)', () => {
    expect(isSettledDecisionVerdict('advanced')).toBe(true);
    expect(isSettledDecisionVerdict('found')).toBe(true);
    expect(isSettledDecisionVerdict('clear')).toBe(true);
  });

  it('rejects interrupted and unevidenced non-decisions', () => {
    expect(isSettledDecisionVerdict('interrupted')).toBe(false);
    expect(isSettledDecisionVerdict('unevidenced')).toBe(false);
  });

  it('rejects unknown / malformed verdict strings', () => {
    expect(isSettledDecisionVerdict('')).toBe(false);
    expect(isSettledDecisionVerdict('Advanced')).toBe(false);
    expect(isSettledDecisionVerdict('nonsense')).toBe(false);
  });

  it('filters interrupted/unevidenced entries out of the rendered register', () => {
    // Simulate the handler's admission gate: only settled verdicts are pushed to
    // the durable register. A mix of tick outcomes narrows to just the decisions.
    const allTickVerdicts: DecisionsRegisterEntry[] = [
      { atTick: 1, at: '2026-07-10T12:00:00Z', surfaceKey: 'direction:d-1', verdict: 'found', note: 'found a lead' },
      { atTick: 2, at: '2026-07-10T12:05:00Z', surfaceKey: 'qualification', verdict: 'interrupted', note: 'turn interrupted before completion' },
      { atTick: 3, at: '2026-07-10T12:10:00Z', surfaceKey: 'direction:d-1', verdict: 'advanced', note: 'spawned a plan task' },
      { atTick: 4, at: '2026-07-10T12:15:00Z', surfaceKey: 'ci-compliance', verdict: 'unevidenced', note: 'no valid TICK-VERDICT marker line' },
      { atTick: 5, at: '2026-07-10T12:20:00Z', surfaceKey: 'vm-cost', verdict: 'clear', note: 'nothing actionable' },
    ];
    const register = allTickVerdicts.filter(e => isSettledDecisionVerdict(e.verdict));
    expect(register).toHaveLength(3);

    const text = decisionsRegisterText(register, 20);
    expect(text).toContain('tick-1 [found]');
    expect(text).toContain('tick-3 [advanced]');
    expect(text).toContain('tick-5 [clear]');
    expect(text).not.toContain('[interrupted]');
    expect(text).not.toContain('[unevidenced]');
  });
});

describe('parseOperatorDecision (operator-settled corrections)', () => {
  it('extracts the trailing OPERATOR-DECISION marker note', () => {
    const text = [
      'Understood — I will drop the images-first rewrite.',
      'OPERATOR-DECISION: Do NOT pursue the images-first compute rewrite; operator settled on the table-based path.',
    ].join('\n');
    expect(parseOperatorDecision(text)).toBe(
      'Do NOT pursue the images-first compute rewrite; operator settled on the table-based path.',
    );
  });

  it('returns null when no marker is present', () => {
    expect(parseOperatorDecision('Thanks, that answers my question.')).toBeNull();
  });

  it('ignores a mid-reply mention and reads the LAST marker line', () => {
    const text = [
      'You asked earlier about OPERATOR-DECISION: markers in the transcript.',
      'Here is my actual answer.',
      'OPERATOR-DECISION: Ship the fix behind a flag.',
    ].join('\n');
    expect(parseOperatorDecision(text)).toBe('Ship the fix behind a flag.');
  });

  it('strips a leading dash and is case-insensitive on the label', () => {
    expect(parseOperatorDecision('operator-decision: — reverse the retirement of d-abc')).toBe(
      'reverse the retirement of d-abc',
    );
  });

  it('returns null for an empty note or non-string input', () => {
    expect(parseOperatorDecision('OPERATOR-DECISION:   ')).toBeNull();
    // @ts-expect-error deliberate non-string input
    expect(parseOperatorDecision(undefined)).toBeNull();
  });

  it('tolerates markdown-decorated OPERATOR-DECISION marker lines', () => {
    // Bold-wrapped: leading/trailing ** must not leak into the note.
    expect(parseOperatorDecision('**OPERATOR-DECISION: Ship the fix behind a flag.**'))
      .toBe('Ship the fix behind a flag.');
    // Bullet-prefixed.
    expect(parseOperatorDecision('- OPERATOR-DECISION: Drop the images-first rewrite.'))
      .toBe('Drop the images-first rewrite.');
    // Blockquote + bold, plus a leading dash inside the note.
    expect(parseOperatorDecision('> **OPERATOR-DECISION: — reverse the retirement of d-abc**'))
      .toBe('reverse the retirement of d-abc');
    // Decorated marker mid-turn followed by a markerless closing wrap is still captured.
    const text = [
      '**OPERATOR-DECISION: keep the table-based compute path.**',
      'Let me know if anything else comes up.',
    ].join('\n');
    expect(parseOperatorDecision(text)).toBe('keep the table-based compute path.');
  });

  it('an operator verdict is admitted to the durable register and survives round-trip', () => {
    expect(isSettledDecisionVerdict(OPERATOR_DECISION_VERDICT)).toBe(true);
    const entry: DecisionsRegisterEntry = {
      atTick: 7,
      at: '2026-07-12T09:00:00Z',
      surfaceKey: 'operator:message',
      verdict: OPERATOR_DECISION_VERDICT,
      note: 'Do NOT re-propose the images-first rewrite.',
    };
    const restored = restoreDecisionsReg(serializeDecisionsReg([entry]));
    expect(restored).not.toBeNull();
    expect(restored?.[0].verdict).toBe(OPERATOR_DECISION_VERDICT);
    expect(restored?.[0].note).toBe('Do NOT re-propose the images-first rewrite.');
    expect(decisionsRegisterText([entry], 20)).toContain('[operator] operator:message');
  });
});

describe('pending operator-decision review state machine', () => {
  const startedAt = '2026-07-16T12:00:00Z';

  it('queues multiple markerless exchanges and advances them one at a time', () => {
    let review = mergePendingOperatorDecisionReview(null, ['m-1'], startedAt, 'first exchange');
    review = mergePendingOperatorDecisionReview(review, ['m-2'], startedAt, 'second exchange');
    review = mergePendingOperatorDecisionReview(review, ['m-3'], startedAt, 'third exchange');

    expect(review.messageIds).toEqual(['m-1']);
    expect(review.exchangeText).toBe('first exchange');
    expect(review.queuedExchanges?.map(exchange => exchange.messageIds)).toEqual([
      ['m-2'],
      ['m-3'],
    ]);

    const second = advancePendingOperatorDecisionReview(review);
    expect(second).toMatchObject({
      messageIds: ['m-2'], exchangeText: 'second exchange', attempts: 0,
    });
    expect(second?.queuedExchanges?.map(exchange => exchange.messageIds)).toEqual([['m-3']]);

    if (second === null) throw new Error('expected the second exchange');
    const third = advancePendingOperatorDecisionReview(second);
    expect(third).toMatchObject({
      messageIds: ['m-3'], exchangeText: 'third exchange', attempts: 0,
    });
    expect(third?.queuedExchanges).toBeUndefined();
    if (third === null) throw new Error('expected the third exchange');
    expect(advancePendingOperatorDecisionReview(third)).toBeNull();
  });

  it('ignores duplicate message ids across the current and queued exchanges', () => {
    const current = mergePendingOperatorDecisionReview(null, ['m-1'], startedAt);
    const queued = mergePendingOperatorDecisionReview(current, ['m-2'], startedAt);

    expect(mergePendingOperatorDecisionReview(queued, ['m-1', 'm-2'], startedAt)).toBe(queued);
  });

  it('round-trips the current exchange and ordered queue across a restart', () => {
    let review = mergePendingOperatorDecisionReview(null, ['m-1'], startedAt, ' first exchange ');
    review = mergePendingOperatorDecisionReview(
      review,
      ['m-2'],
      '2026-07-16T12:01:00Z',
      'x'.repeat(500),
    );

    const restored = restoreLedger(serializeLedger([], 4, false, true, review));
    expect(restored?.operatorDecisionReview).toEqual(review);
    expect(restored?.operatorDecisionCorrective).toBe(true);
    expect(restored?.operatorDecisionReview?.queuedExchanges?.[0]).toMatchObject({
      messageIds: ['m-2'],
      exchangeText: 'x'.repeat(384),
      exchangeTextTruncated: true,
      startedAt: '2026-07-16T12:01:00Z',
      attempts: 0,
    });
  });

  it('compacts an oversized queue into retained exchanges plus overflow metadata', () => {
    let review = mergePendingOperatorDecisionReview(null, ['m-current'], startedAt, 'current');
    for (let i = 0; i < 400; i++) {
      review = mergePendingOperatorDecisionReview(
        review,
        [`m-${i}`],
        '2026-07-16T12:01:00Z',
        `queued exchange ${i}`,
      );
    }

    const serialized = serializeLedger([], 1, false, true, review);
    expect(Buffer.byteLength(serialized, 'utf8')).toBeLessThanOrEqual(EXAM_LEDGER_MAX_BYTES);
    const restored = restoreLedger(serialized)?.operatorDecisionReview;
    expect(restored?.overflowed).toBe(true);
    expect(restored?.queuedExchanges?.length).toBeLessThan(400);
    expect(restored?.overflowExchanges).toBe(
      400 - (restored?.queuedExchanges?.length ?? 0),
    );

    if (restored === null || restored === undefined) throw new Error('expected compacted review');
    const afterAnotherExchange = mergePendingOperatorDecisionReview(
      restored,
      ['m-after-overflow'],
      startedAt,
    );
    expect(afterAnotherExchange.queuedExchanges).toEqual(restored.queuedExchanges);
    expect(afterAnotherExchange.overflowExchanges).toBe((restored.overflowExchanges ?? 0) + 1);
  });

  it('falls back to an id-less sentinel when even the active exchange cannot fit', () => {
    const oversized: PendingOperatorDecisionReview = {
      status: 'pending',
      messageIds: ['m-current'],
      exchangeText: 'x'.repeat(EXAM_LEDGER_MAX_BYTES * 2),
      startedAt,
      attempts: 3,
    };

    const serialized = serializeLedger([], 1, false, true, oversized);
    expect(Buffer.byteLength(serialized, 'utf8')).toBeLessThanOrEqual(EXAM_LEDGER_MAX_BYTES);
    expect(restoreLedger(serialized)?.operatorDecisionReview).toEqual({
      status: 'pending',
      messageIds: [],
      exchangeTextTruncated: true,
      startedAt: '',
      attempts: 3,
      overflowed: true,
      overflowExchanges: 1,
    });
  });

  it('keeps an overflow sentinel until an explicit overflow outcome covers it', () => {
    const withExplicitCurrent: PendingOperatorDecisionReview = {
      status: 'pending', messageIds: ['m-1'], startedAt, attempts: 0, overflowed: true,
      overflowExchanges: 2,
    };
    const sentinel = advancePendingOperatorDecisionReview(withExplicitCurrent);
    expect(sentinel).toMatchObject({
      messageIds: [], overflowed: true, overflowExchanges: 2,
    });
    if (sentinel === null) throw new Error('expected overflow sentinel');
    expect(advancePendingOperatorDecisionReview(sentinel)).toBe(sentinel);
    expect(advancePendingOperatorDecisionReview(sentinel, { coversOverflow: false })).toBe(sentinel);
    expect(advancePendingOperatorDecisionReview(sentinel, { coversOverflow: true })).toBeNull();
  });

  it('falls back to admitting exchange text after attempt threshold', () => {
    // Verify that the library primitives support the fallback path implemented in
    // strategic-resident.ts: after MAX_REVIEW_ATTEMPTS consecutive retain calls on
    // a stalled review, the handler force-admits the available exchange text as an
    // operator-verdict entry and clears the review queue.
    const exchangeText = 'do not pursue the images-first compute rewrite';
    const review = mergePendingOperatorDecisionReview(null, ['m-1'], startedAt, exchangeText);

    // Simulate MAX_REVIEW_ATTEMPTS consecutive retain calls (missed markers).
    let retained = review;
    for (let i = 0; i < MAX_REVIEW_ATTEMPTS; i++) {
      expect(retained.attempts).toBeLessThan(MAX_REVIEW_ATTEMPTS);
      retained = retainPendingOperatorDecisionReview(retained);
    }

    // The handler checks this condition to trigger the fallback.
    expect(retained.attempts).toBeGreaterThanOrEqual(MAX_REVIEW_ATTEMPTS);

    // Simulate the handler's force-admit: build the fallback note from exchangeText.
    const fallbackNote = retained.exchangeText
      ? `[unclassified after ${retained.attempts} attempts] ${retained.exchangeText}`
      : `[unclassified after ${retained.attempts} attempts — exchange text unavailable]`;
    const register: DecisionsRegisterEntry[] = [];
    admitDecisionEntry(register, {
      atTick: 10,
      at: startedAt,
      surfaceKey: 'operator:message',
      verdict: OPERATOR_DECISION_VERDICT,
      note: fallbackNote,
    }, DECISIONS_REGISTER_CAP);

    // The admission must produce exactly one operator entry containing the exchange text.
    expect(register).toHaveLength(1);
    expect(register[0].verdict).toBe(OPERATOR_DECISION_VERDICT);
    expect(register[0].note).toContain(exchangeText);
    expect(register[0].note).toContain(`${MAX_REVIEW_ATTEMPTS} attempts`);

    // After admission the review should be advanced (cleared) so the queue drains.
    const advanced = advancePendingOperatorDecisionReview(review);
    expect(advanced).toBeNull(); // no queued exchanges — queue is now empty

    // The admitted entry survives a serialize→restore round-trip so the correction
    // is not lost at restart or compaction.
    const restored = restoreDecisionsReg(serializeDecisionsReg(register));
    expect(restored?.some(e => e.verdict === OPERATOR_DECISION_VERDICT && e.note.includes(exchangeText))).toBe(true);
  });
});

describe('classifyOperatorDecisionReview', () => {
  it('classifies valid ordinary and explicit overflow outcomes', () => {
    expect(classifyOperatorDecisionReview({
      resultText: 'OPERATOR-DECISION: Keep the table-based path.',
    })).toEqual({ kind: 'recovered', note: 'Keep the table-based path.' });
    expect(classifyOperatorDecisionReview({
      resultText: 'OPERATOR-DECISION-REVIEW: NO-DECISION',
    })).toEqual({ kind: 'no-decision' });
    expect(classifyOperatorDecisionReview({
      resultText: 'OPERATOR-DECISION-REVIEW: OVERFLOW-NO-DECISION',
    })).toEqual({ kind: 'no-decision', coversOverflow: true });
    expect(classifyOperatorDecisionReview({
      resultText: 'OPERATOR-DECISION-REVIEW: OVERFLOW-DECISION: Keep A; reject B.',
    })).toEqual({ kind: 'recovered', note: 'Keep A; reject B.', coversOverflow: true });
  });

  it('deduplicates the same streamed marker across final and accumulated text', () => {
    expect(classifyOperatorDecisionReview({
      resultText: 'OPERATOR-DECISION-REVIEW: NO-DECISION',
      accumulatedTurnText: 'preamble\nOPERATOR-DECISION-REVIEW: NO-DECISION',
    })).toEqual({ kind: 'no-decision' });
  });

  it.each([
    ['missing', 'ordinary prose'],
    ['empty decision', 'OPERATOR-DECISION:   '],
    ['malformed review', 'OPERATOR-DECISION-REVIEW: MAYBE'],
    ['empty overflow decision', 'OPERATOR-DECISION-REVIEW: OVERFLOW-DECISION:   '],
    ['conflicting', 'OPERATOR-DECISION: Keep A.\nOPERATOR-DECISION-REVIEW: NO-DECISION'],
    ['duplicate', 'OPERATOR-DECISION-REVIEW: NO-DECISION\nOPERATOR-DECISION-REVIEW: NO-DECISION'],
  ])('retains the review for %s markers', (_case, resultText) => {
    const outcome = classifyOperatorDecisionReview({ resultText });
    expect(outcome.kind).toBe('pending');
    expect(outcome.statusNote).toContain(
      _case === 'missing' ? 'missing a valid resolution marker' : 'conflicting, duplicate, or malformed',
    );
  });

  it('retains an interrupted review even when the output contains a valid marker', () => {
    expect(classifyOperatorDecisionReview({
      resultText: 'OPERATOR-DECISION-REVIEW: NO-DECISION',
      interrupted: true,
    })).toEqual({
      kind: 'pending',
      statusNote: 'operator-decision review interrupted — durable review remains pending',
    });
  });
});

describe('buildBootstrapPrompt operator-decision instruction', () => {
  const base = {
    contextPrefix: '', projectName: 'Blueprint', problemModelJson: '{}',
    runningUpdate: '', directionsText: '', stagedDirectionsText: '',
    retiredDirectionsText: '', previousDecisionOutcomesText: '',
    strategicMemoryNotesText: '', visionJson: '', openMessagesText: '',
    sessionId: 's-1', fetchGuidanceText: '', priorSessionContextText: '',
    skillsText: '', decisionsRegisterText: '',
  };
  it('instructs the strategist to emit an OPERATOR-DECISION marker on a settlement', () => {
    const p = buildBootstrapPrompt(base);
    expect(p).toContain('OPERATOR-DECISION:');
    expect(p).toContain('durable decisions register');
  });
});

describe('serializeDecisionsReg / restoreDecisionsReg round-trip', () => {
  const makeEntry = (tick: number, key: string): DecisionsRegisterEntry => ({
    atTick: tick,
    at: `2026-07-10T12:0${tick}:00Z`,
    surfaceKey: key,
    verdict: 'found',
    note: `Settled decision at tick ${tick} — full-fidelity text, not truncated`,
  });

  it('test_decisions_reg_roundtrip_restores_entries_across_restart', () => {
    const reg = [
      makeEntry(1, 'direction:d-abc'),
      makeEntry(2, 'qualification'),
      makeEntry(3, 'direction:d-xyz'),
    ];
    const serialized = serializeDecisionsReg(reg);
    // Must be valid JSON.
    const parsed: unknown = JSON.parse(serialized);
    expect(parsed).toBeTruthy();
    const restored = restoreDecisionsReg(serialized);
    expect(restored).not.toBeNull();
    expect(restored).toHaveLength(3);
    if (restored !== null) {
      expect(restored[0].atTick).toBe(1);
      expect(restored[0].surfaceKey).toBe('direction:d-abc');
      expect(restored[0].note).toContain('full-fidelity text');
      expect(restored[2].surfaceKey).toBe('direction:d-xyz');
    }
  });

  it('test_decisions_reg_restore_returns_null_on_bad_input', () => {
    expect(restoreDecisionsReg(null)).toBeNull();
    expect(restoreDecisionsReg(undefined)).toBeNull();
    expect(restoreDecisionsReg('')).toBeNull();
    expect(restoreDecisionsReg('not-json')).toBeNull();
    // Non-array entries field degrades to empty array (graceful fallback, not null).
    expect(restoreDecisionsReg('{"entries":"not-array"}')).toEqual([]);
  });

  it('test_decisions_reg_serialize_empty_register_restores_to_empty_array', () => {
    const serialized = serializeDecisionsReg([]);
    const restored = restoreDecisionsReg(serialized);
    expect(restored).toEqual([]);
  });

  it('test_decisions_reg_serialize_fits_within_byte_budget', () => {
    // Build a register with notes long enough to stress the byte budget.
    const bigReg: DecisionsRegisterEntry[] = Array.from({ length: DECISIONS_REGISTER_CAP }, (_, i) => ({
      atTick: i,
      at: `2026-07-10T12:00:00Z`,
      surfaceKey: `direction:d-${i}`,
      verdict: 'found',
      note: 'x'.repeat(500),
    }));
    const serialized = serializeDecisionsReg(bigReg);
    expect(Buffer.byteLength(serialized, 'utf8')).toBeLessThanOrEqual(DECISIONS_REG_MAX_BYTES);
    const restored = restoreDecisionsReg(serialized);
    expect(restored).not.toBeNull();
    // All restored entries must be valid.
    for (const e of restored ?? []) {
      expect(typeof e.surfaceKey).toBe('string');
      expect(typeof e.note).toBe('string');
    }
  });
});

describe('admitDecisionEntry (priority-aware decisions-register eviction)', () => {
  const entry = (
    tick: number,
    verdict: string,
    key = `surface:${tick}`,
  ): DecisionsRegisterEntry => ({
    atTick: tick,
    at: `2026-07-10T12:00:${String(tick).padStart(2, '0')}Z`,
    surfaceKey: key,
    verdict,
    note: `note-${tick}`,
  });

  it('operator decision survives a flood of clear verdicts in the decisions register', () => {
    // The named-test proof for this direction: an operator correction recorded
    // early must NOT be evicted by the designed-common flood of autonomous
    // `clear` verdicts that follows. A pure FIFO cap would drop it once the
    // clears exceed the cap; the priority-aware admission must preserve it.
    const register: DecisionsRegisterEntry[] = [];
    admitDecisionEntry(register, entry(1, 'operator', 'operator:message'), DECISIONS_REGISTER_CAP);

    // Flood the register with far more clears than the cap.
    for (let i = 0; i < DECISIONS_REGISTER_CAP * 3; i++) {
      admitDecisionEntry(register, entry(100 + i, 'clear'), DECISIONS_REGISTER_CAP);
    }

    expect(register.length).toBeLessThanOrEqual(DECISIONS_REGISTER_CAP);
    // The scarce operator settlement is still present…
    const operatorEntries = register.filter(e => e.verdict === 'operator');
    expect(operatorEntries).toHaveLength(1);
    expect(operatorEntries[0].surfaceKey).toBe('operator:message');
    // …and it survives a serialize→restore restart round-trip.
    const restored = restoreDecisionsReg(serializeDecisionsReg(register));
    expect(restored?.some(e => e.verdict === 'operator')).toBe(true);
  });

  it('never evicts protected operator/advanced/found entries while any clear remains', () => {
    const register: DecisionsRegisterEntry[] = [];
    // Seed one of each protected verdict at the front (oldest positions).
    admitDecisionEntry(register, entry(1, 'operator'), DECISIONS_REGISTER_CAP);
    admitDecisionEntry(register, entry(2, 'advanced'), DECISIONS_REGISTER_CAP);
    admitDecisionEntry(register, entry(3, 'found'), DECISIONS_REGISTER_CAP);
    // Then a flood of clears.
    for (let i = 0; i < DECISIONS_REGISTER_CAP * 2; i++) {
      admitDecisionEntry(register, entry(100 + i, 'clear'), DECISIONS_REGISTER_CAP);
    }
    expect(register.length).toBe(DECISIONS_REGISTER_CAP);
    expect(register.filter(e => e.verdict === 'operator')).toHaveLength(1);
    expect(register.filter(e => e.verdict === 'advanced')).toHaveLength(1);
    expect(register.filter(e => e.verdict === 'found')).toHaveLength(1);
  });

  it('evicts the OLDEST clear first when over cap', () => {
    const register: DecisionsRegisterEntry[] = [];
    // Fill to cap entirely with clears, oldest tick first.
    for (let i = 0; i < DECISIONS_REGISTER_CAP; i++) {
      admitDecisionEntry(register, entry(i, 'clear'), DECISIONS_REGISTER_CAP);
    }
    // One more clear pushes out the oldest (tick 0), keeping tick 1..cap.
    admitDecisionEntry(register, entry(999, 'clear'), DECISIONS_REGISTER_CAP);
    expect(register.length).toBe(DECISIONS_REGISTER_CAP);
    expect(register.some(e => e.atTick === 0)).toBe(false);
    expect(register.some(e => e.atTick === 999)).toBe(true);
  });

  it('falls back to evicting the oldest overall when no clear remains', () => {
    const register: DecisionsRegisterEntry[] = [];
    // Fill to cap with protected (found) entries, then admit one more.
    for (let i = 0; i < DECISIONS_REGISTER_CAP; i++) {
      admitDecisionEntry(register, entry(i, 'found'), DECISIONS_REGISTER_CAP);
    }
    admitDecisionEntry(register, entry(999, 'operator'), DECISIONS_REGISTER_CAP);
    expect(register.length).toBe(DECISIONS_REGISTER_CAP);
    // Oldest overall (tick 0) was evicted; newest operator entry retained.
    expect(register.some(e => e.atTick === 0)).toBe(false);
    expect(register.some(e => e.atTick === 999 && e.verdict === 'operator')).toBe(true);
  });

  it('a new clear cannot displace a protected entry when the register is all-protected at cap', () => {
    const register: DecisionsRegisterEntry[] = [];
    for (let i = 0; i < DECISIONS_REGISTER_CAP; i++) {
      admitDecisionEntry(register, entry(i, 'operator'), DECISIONS_REGISTER_CAP);
    }
    // The incoming clear is itself the only clear, so it is evicted — no
    // operator entry is lost.
    admitDecisionEntry(register, entry(999, 'clear'), DECISIONS_REGISTER_CAP);
    expect(register.length).toBe(DECISIONS_REGISTER_CAP);
    expect(register.filter(e => e.verdict === 'operator')).toHaveLength(DECISIONS_REGISTER_CAP);
    expect(register.some(e => e.atTick === 999)).toBe(false);
  });

  it('serializeDecisionsReg drops clears before operator entries when over the count cap', () => {
    // An over-cap register (clears at the front, one operator at the back) must
    // serialize with the operator entry retained — not front-dropped by age.
    const register: DecisionsRegisterEntry[] = [];
    for (let i = 0; i < DECISIONS_REGISTER_CAP; i++) register.push(entry(i, 'clear'));
    register.push(entry(999, 'operator'));
    const restored = restoreDecisionsReg(serializeDecisionsReg(register));
    expect(restored).not.toBeNull();
    expect(restored?.length).toBeLessThanOrEqual(DECISIONS_REGISTER_CAP);
    expect(restored?.some(e => e.verdict === 'operator')).toBe(true);
  });
});

describe('buildSurfaceCatalog', () => {
  it('derives direction, diagnostic-fetch, standing, and code:<dir> surfaces', () => {
    const catalog = buildSurfaceCatalog(
      { directions_summary: [
        { id: 'd-1', text: 'first direction' },
        { id: 'd-2', text: 'second' },
        { text: 'no-id — skipped' },
      ] },
      ['backend', 'agent', 'admin-ui'],
    );
    const keys = catalog.map(s => s.key);
    expect(keys).toContain('direction:d-1');
    expect(keys).toContain('direction:d-2');
    expect(keys).not.toContain('direction:'); // the id-less direction is skipped
    // Diagnostic fetch surfaces from the STRATEGIC_FETCH_VERBS subset.
    expect(keys).toContain('qualification');
    expect(keys).toContain('ci-compliance');
    // Standing surfaces.
    expect(keys).toContain('messages:recent');
    expect(keys).toContain('knowledge:recent');
    expect(keys).toContain('problem-model');
    expect(keys).toContain('design-doc');
    // Code surfaces seeded from the workspace top-level dirs (project-agnostic).
    expect(keys).toContain('code:backend');
    expect(keys).toContain('code:agent');
    expect(keys).toContain('code:admin-ui');
    // A direction surface carries a fetch hint naming its id.
    const d1 = catalog.find(s => s.key === 'direction:d-1');
    expect(d1?.kind).toBe('direction');
    expect(d1?.hint).toContain('./fetch direction d-1');
  });

  it('is tolerant of a missing directions_summary and empty workspace dirs', () => {
    const catalog = buildSurfaceCatalog({}, []);
    expect(catalog.some(s => s.key === 'qualification')).toBe(true);
    expect(catalog.some(s => s.kind === 'code')).toBe(false);
    expect(catalog.some(s => s.kind === 'direction')).toBe(false);
  });

  it('tracks the payload: new directions enter, dropped ones are absent', () => {
    const before = buildSurfaceCatalog({ directions_summary: [{ id: 'd-1' }] }, []);
    expect(before.map(s => s.key)).toContain('direction:d-1');
    const after = buildSurfaceCatalog({ directions_summary: [{ id: 'd-2' }] }, []);
    expect(after.map(s => s.key)).toContain('direction:d-2');
    expect(after.map(s => s.key)).not.toContain('direction:d-1'); // retired → pruned
  });
});

describe('nextFocus (never-examined first, then LRU)', () => {
  const catalog: ExamSurface[] = [
    { key: 'a', kind: 'fetch', label: 'a', hint: 'ha' },
    { key: 'b', kind: 'fetch', label: 'b', hint: 'hb' },
    { key: 'c', kind: 'fetch', label: 'c', hint: 'hc' },
    { key: 'd', kind: 'fetch', label: 'd', hint: 'hd' },
  ];

  it('returns never-examined surfaces first, in catalog order', () => {
    const records: ExamRecord[] = [
      { surfaceKey: 'a', atTick: 1, at: 't', verdict: 'clear', note: '' },
    ];
    // b, c, d never examined → first (catalog order); a is examined → last.
    expect(nextFocus(catalog, records, 4).map(s => s.key)).toEqual(['b', 'c', 'd', 'a']);
  });

  it('orders examined surfaces least-recently-first (LRU)', () => {
    const records: ExamRecord[] = [
      { surfaceKey: 'a', atTick: 5, at: 't', verdict: 'clear', note: '' },
      { surfaceKey: 'b', atTick: 2, at: 't', verdict: 'clear', note: '' },
      { surfaceKey: 'c', atTick: 9, at: 't', verdict: 'clear', note: '' },
      { surfaceKey: 'd', atTick: 1, at: 't', verdict: 'clear', note: '' },
    ];
    // All examined → pure LRU by max atTick: d(1) < b(2) < a(5) < c(9).
    expect(nextFocus(catalog, records, 4).map(s => s.key)).toEqual(['d', 'b', 'a', 'c']);
  });

  it('uses the MAX atTick per surface and honors n', () => {
    const records: ExamRecord[] = [
      { surfaceKey: 'a', atTick: 1, at: 't', verdict: 'clear', note: '' },
      { surfaceKey: 'a', atTick: 8, at: 't', verdict: 'found', note: '' }, // a's recency = 8
      { surfaceKey: 'b', atTick: 3, at: 't', verdict: 'clear', note: '' },
      { surfaceKey: 'c', atTick: 4, at: 't', verdict: 'clear', note: '' },
      { surfaceKey: 'd', atTick: 5, at: 't', verdict: 'clear', note: '' },
    ];
    // b(3) < c(4) < d(5) < a(8); n=2 → the two stalest.
    expect(nextFocus(catalog, records, 2).map(s => s.key)).toEqual(['b', 'c']);
  });

  it('ignores records for surfaces no longer in the catalog', () => {
    const records: ExamRecord[] = [
      { surfaceKey: 'gone', atTick: 99, at: 't', verdict: 'clear', note: '' },
    ];
    // Every catalog surface is still never-examined despite the stale record.
    expect(nextFocus(catalog, records, 4).map(s => s.key)).toEqual(['a', 'b', 'c', 'd']);
  });
});

describe('parseTickVerdict', () => {
  it('parses ADVANCED (no surface key)', () => {
    expect(parseTickVerdict('did stuff\nTICK-VERDICT: ADVANCED — spawned a plan task for d-1'))
      .toEqual({ verdict: 'advanced', surfaceKey: '', note: 'spawned a plan task for d-1' });
  });

  it('parses FOUND <surface> — note without splitting a hyphenated key', () => {
    expect(parseTickVerdict('TICK-VERDICT: FOUND direction:d-21efb120 — churn is high, needs review'))
      .toEqual({ verdict: 'found', surfaceKey: 'direction:d-21efb120', note: 'churn is high, needs review' });
  });

  it('parses CLEAR <surface> — note', () => {
    expect(parseTickVerdict('TICK-VERDICT: CLEAR code:backend — read orchestrator.py, nothing actionable'))
      .toEqual({ verdict: 'clear', surfaceKey: 'code:backend', note: 'read orchestrator.py, nothing actionable' });
  });

  it('uses the LAST marker and is case-insensitive', () => {
    const text = 'tick-verdict: clear a — early\nmore\nTICK-VERDICT: FOUND b — later';
    expect(parseTickVerdict(text)).toEqual({ verdict: 'found', surfaceKey: 'b', note: 'later' });
  });

  it('handles a FOUND/CLEAR with a space separator (no dash)', () => {
    expect(parseTickVerdict('TICK-VERDICT: CLEAR qualification no findings'))
      .toEqual({ verdict: 'clear', surfaceKey: 'qualification', note: 'no findings' });
  });

  it('returns null for a missing or garbled marker', () => {
    expect(parseTickVerdict('just some prose, no marker at all')).toBeNull();
    expect(parseTickVerdict('TICK-VERDICT: MAYBE something')).toBeNull();
    expect(parseTickVerdict('')).toBeNull();
    // @ts-expect-error — defensive: non-string input degrades to null.
    expect(parseTickVerdict(null)).toBeNull();
  });

  it('parseTickVerdict tolerates markdown-decorated marker lines', () => {
    // Bold-wrapped CLEAR: leading/trailing ** must not leak into surface/note.
    expect(parseTickVerdict('**TICK-VERDICT: CLEAR code:backend — looks fine**'))
      .toEqual({ verdict: 'clear', surfaceKey: 'code:backend', note: 'looks fine' });
    // Bullet-prefixed ADVANCED.
    expect(parseTickVerdict('- TICK-VERDICT: ADVANCED — spawned a plan task'))
      .toEqual({ verdict: 'advanced', surfaceKey: '', note: 'spawned a plan task' });
    // Blockquote-prefixed FOUND keeps the hyphenated surface key intact.
    expect(parseTickVerdict('> TICK-VERDICT: FOUND direction:d-21efb120 — lead here'))
      .toEqual({ verdict: 'found', surfaceKey: 'direction:d-21efb120', note: 'lead here' });
    // Blockquote + bold together, italic single-star wrap, and no stray decoration.
    expect(parseTickVerdict('> **TICK-VERDICT: CLEAR qualification — nothing actionable**'))
      .toEqual({ verdict: 'clear', surfaceKey: 'qualification', note: 'nothing actionable' });
    expect(parseTickVerdict('*TICK-VERDICT: ADVANCED — did the thing*'))
      .toEqual({ verdict: 'advanced', surfaceKey: '', note: 'did the thing' });
  });

  it('captures a decorated mid-turn marker even when the turn ends markerless (d-ca17d9cb)', () => {
    // The decorated marker appears before a plain closing wrap; last-line scan
    // must still find it rather than returning null.
    const text = [
      '**TICK-VERDICT: CLEAR code:backend — read orchestrator.py, nothing actionable**',
      '',
      'That concludes this tick — resuming rotation.',
    ].join('\n');
    expect(parseTickVerdict(text))
      .toEqual({ verdict: 'clear', surfaceKey: 'code:backend', note: 'read orchestrator.py, nothing actionable' });
  });

  it('still ignores an inline TICK-VERDICT mention that is not line-leading', () => {
    expect(parseTickVerdict('I will note the TICK-VERDICT: CLEAR convention next time.')).toBeNull();
  });
});

describe('parseAgendaContinue', () => {
  it('detects a literal AGENDA-CONTINUE line', () => {
    expect(parseAgendaContinue('did work\nAGENDA-CONTINUE\nTICK-VERDICT: ADVANCED — x')).toBe(true);
    expect(parseAgendaContinue('no more to do here')).toBe(false);
  });

  it('tolerates markdown-decorated AGENDA-CONTINUE lines', () => {
    expect(parseAgendaContinue('did work\n- AGENDA-CONTINUE\nmore')).toBe(true);
    expect(parseAgendaContinue('did work\n> **AGENDA-CONTINUE**\nmore')).toBe(true);
    // An inline mention that is not line-leading must not trip it.
    expect(parseAgendaContinue('I considered AGENDA-CONTINUE but there is no more work.')).toBe(false);
  });
});

describe('openFoundLeads', () => {
  it('keeps FOUND surfaces whose latest record is still FOUND', () => {
    const records: ExamRecord[] = [
      { surfaceKey: 'a', atTick: 1, at: 't', verdict: 'found', note: 'lead-a' },
      { surfaceKey: 'b', atTick: 2, at: 't', verdict: 'found', note: 'lead-b' },
      { surfaceKey: 'a', atTick: 3, at: 't', verdict: 'advanced', note: 'acted on a' }, // closes a
    ];
    expect(openFoundLeads(records).map(r => r.surfaceKey)).toEqual(['b']);
  });
});

describe('classifyTickOutcome (unevidenced / corrective path)', () => {
  const focus: ExamSurface[] = [
    { key: 'code:backend', kind: 'code', label: 'code/backend', hint: 'read backend' },
  ];

  it('records an evidenced CLEAR and does NOT set corrective', () => {
    const upd = classifyTickOutcome({
      resultText: 'TICK-VERDICT: CLEAR code:backend — read orchestrator.py, nothing actionable',
      sawToolUse: true, focus, atTick: 4, at: 'ts',
    });
    expect(upd.record.verdict).toBe('clear');
    expect(upd.record.surfaceKey).toBe('code:backend');
    expect(upd.corrective).toBe(false);
    expect(upd.statusNote).toBeUndefined();
  });

  it('records a CLEAR-with-no-tool-use as unevidenced and sets corrective', () => {
    const upd = classifyTickOutcome({
      resultText: 'TICK-VERDICT: CLEAR code:backend — looks fine',
      sawToolUse: false, focus, atTick: 5, at: 'ts',
    });
    expect(upd.record.verdict).toBe('unevidenced');
    expect(upd.corrective).toBe(true);
    expect(upd.statusNote).toContain('examined nothing');
  });

  it('records a missing/garbled marker as unevidenced and sets corrective', () => {
    const upd = classifyTickOutcome({
      resultText: 'I looked around and everything seems fine.',
      sawToolUse: true, focus, atTick: 6, at: 'ts',
    });
    expect(upd.record.verdict).toBe('unevidenced');
    expect(upd.record.surfaceKey).toBe('code:backend'); // falls back to stalest focus
    expect(upd.corrective).toBe(true);
    expect(upd.statusNote).toContain('without a valid TICK-VERDICT');
  });

  it('records ADVANCED against the stalest focus surface and carries AGENDA-CONTINUE', () => {
    const upd = classifyTickOutcome({
      resultText: 'AGENDA-CONTINUE\nTICK-VERDICT: ADVANCED — spawned a plan task',
      sawToolUse: true, focus, atTick: 7, at: 'ts',
    });
    expect(upd.record.verdict).toBe('advanced');
    expect(upd.record.surfaceKey).toBe('code:backend');
    expect(upd.corrective).toBe(false);
    expect(upd.agendaContinue).toBe(true);
  });

  it('classifyTickOutcome uses accumulated markers only when final result is markerless', () => {
    const upd = classifyTickOutcome({
      resultText: 'closing tool summary',
      accumulatedTurnText: '> **AGENDA-CONTINUE**\n**TICK-VERDICT: ADVANCED — streamed marker survived**',
      sawToolUse: true, focus, atTick: 7, at: 'ts',
    });
    expect(upd.record).toMatchObject({ verdict: 'advanced', note: 'streamed marker survived' });
    expect(upd.agendaContinue).toBe(true);
  });

  it('prefers a final-result marker over a conflicting accumulated marker', () => {
    const upd = classifyTickOutcome({
      resultText: 'TICK-VERDICT: CLEAR code:backend — final result wins',
      accumulatedTurnText: 'TICK-VERDICT: FOUND direction:old — stale stream marker',
      sawToolUse: true, focus, atTick: 7, at: 'ts',
    });
    expect(upd.record).toMatchObject({ verdict: 'clear', note: 'final result wins' });
  });

  it('uses accumulated AGENDA-CONTINUE when the final result has only a verdict', () => {
    const upd = classifyTickOutcome({
      resultText: 'TICK-VERDICT: ADVANCED — final marker',
      accumulatedTurnText: 'AGENDA-CONTINUE',
      sawToolUse: true, focus, atTick: 7, at: 'ts',
    });
    expect(upd.agendaContinue).toBe(true);
  });

  it('classifyTickOutcome does not mark interrupted turns corrective', () => {
    // An interrupted turn (operator preemption, authority suspend, stall watchdog)
    // produces empty resultText and sawToolUse=false — no TICK-VERDICT marker was
    // ever written. With interrupted:true, classifyTickOutcome must NOT record the
    // turn as unevidenced or set the corrective flag — that would create a spurious
    // contract violation for something the agent never had a chance to finish.
    const upd = classifyTickOutcome({
      resultText: '',
      sawToolUse: false,
      focus,
      atTick: 8,
      at: 'ts',
      interrupted: true,
    });
    expect(upd.record.verdict).toBe('interrupted');
    expect(upd.corrective).toBe(false);
    expect(upd.agendaContinue).toBe(false);
    expect(upd.statusNote).toBeUndefined();
  });

  it('interrupted turn records the stalest focus surface as surfaceKey', () => {
    const upd = classifyTickOutcome({
      resultText: 'partial output before interrupt',
      sawToolUse: true,
      focus,
      atTick: 9,
      at: 'ts',
      interrupted: true,
    });
    expect(upd.record.surfaceKey).toBe('code:backend');
    expect(upd.record.verdict).toBe('interrupted');
    expect(upd.corrective).toBe(false);
  });

  it('non-interrupted CLEAR-with-no-tool-use is still corrective (not suppressed by interrupted path)', () => {
    // Confirm the interrupted:false (default) path does not weaken the CLEAR check.
    const upd = classifyTickOutcome({
      resultText: 'TICK-VERDICT: CLEAR code:backend — looks fine',
      sawToolUse: false,
      focus,
      atTick: 10,
      at: 'ts',
      interrupted: false,
    });
    expect(upd.record.verdict).toBe('unevidenced');
    expect(upd.corrective).toBe(true);
  });

  it('non-interrupted missing marker is still corrective', () => {
    const upd = classifyTickOutcome({
      resultText: 'I looked around and nothing needs doing.',
      sawToolUse: true,
      focus,
      atTick: 11,
      at: 'ts',
      interrupted: false,
    });
    expect(upd.record.verdict).toBe('unevidenced');
    expect(upd.corrective).toBe(true);
    expect(upd.statusNote).toContain('without a valid TICK-VERDICT');
  });
});

describe('shouldRunAutonomousTurn (state-gated pacing)', () => {
  const base = {
    currentWorkStateRev: undefined as string | null | undefined,
    lastTurnWorkStateRev: null as string | null,
    previousTurnAgendaContinue: false,
    operatorTurnJustCompleted: false,
    msSinceLastAutonomousTurn: 0,
    floorTickMs: AUTONOMOUS_FLOOR_TICK_MS,
  };

  it('fires when an operator turn just completed', () => {
    expect(shouldRunAutonomousTurn({ ...base, operatorTurnJustCompleted: true })).toBe(true);
  });

  it('fires when the previous turn ended AGENDA-CONTINUE', () => {
    expect(shouldRunAutonomousTurn({ ...base, previousTurnAgendaContinue: true })).toBe(true);
  });

  it('fires when work_state_rev differs from the rev at the last turn', () => {
    expect(shouldRunAutonomousTurn({ ...base, currentWorkStateRev: 'r2', lastTurnWorkStateRev: 'r1' })).toBe(true);
    expect(shouldRunAutonomousTurn({ ...base, currentWorkStateRev: 'r1', lastTurnWorkStateRev: 'r1' })).toBe(false);
  });

  it('fires when the floor tick has elapsed', () => {
    expect(shouldRunAutonomousTurn({ ...base, msSinceLastAutonomousTurn: AUTONOMOUS_FLOOR_TICK_MS })).toBe(true);
    expect(shouldRunAutonomousTurn({ ...base, msSinceLastAutonomousTurn: AUTONOMOUS_FLOOR_TICK_MS - 1 })).toBe(false);
  });

  it('degrades to floor-tick pacing when work_state_rev is absent (older backend)', () => {
    // No rev signal + floor not elapsed + no other trigger → do NOT fire.
    expect(shouldRunAutonomousTurn({ ...base, currentWorkStateRev: undefined, msSinceLastAutonomousTurn: 10 })).toBe(false);
    // ...but the floor tick still fires it.
    expect(shouldRunAutonomousTurn({ ...base, currentWorkStateRev: undefined, msSinceLastAutonomousTurn: AUTONOMOUS_FLOOR_TICK_MS })).toBe(true);
  });

  it('runs the first-ever tick (Infinity since last) via the floor', () => {
    expect(shouldRunAutonomousTurn({ ...base, msSinceLastAutonomousTurn: Number.POSITIVE_INFINITY })).toBe(true);
  });

  it('shouldRunAutonomousTurn does not fire on the loop\'s own applied ops', () => {
    // Simulate the corrected post-turn baseline recording: lastTurnWorkStateRev is
    // set to currentWorkStateRev AFTER the post-turn heartbeat, so the rev already
    // includes the loop's own applied op. The next tick must NOT fire purely because
    // of that self-echo — the gate should see no change.
    const revAfterOwnOp = 'r-after-own-op';
    expect(shouldRunAutonomousTurn({
      ...base,
      currentWorkStateRev: revAfterOwnOp,
      lastTurnWorkStateRev: revAfterOwnOp, // baseline already folded in own writes
      msSinceLastAutonomousTurn: 0, // floor not elapsed
    })).toBe(false);

    // But a GENUINE external change (different rev since the loop set its baseline)
    // must still fire the gate.
    const revFromExternalChange = 'r-external';
    expect(shouldRunAutonomousTurn({
      ...base,
      currentWorkStateRev: revFromExternalChange,
      lastTurnWorkStateRev: revAfterOwnOp, // baseline is stale vs external change
      msSinceLastAutonomousTurn: 0,
    })).toBe(true);
  });
});

describe('nextAuthorityHeld / autonomousActive', () => {
  it('SUSPENDS on authority_held:false without a stop', () => {
    // Backend lapsed the lease: authority gone, but the session is still ACTIVE.
    expect(nextAuthorityHeld(true, { authority_held: false, stop_requested: false, status: 'ACTIVE' })).toBe(false);
  });

  it('RESUMES on authority_held:true after a lapse', () => {
    expect(nextAuthorityHeld(false, { authority_held: true })).toBe(true);
  });

  it('leaves the state unchanged when the field is absent (older backend)', () => {
    expect(nextAuthorityHeld(true, {})).toBe(true);
    expect(nextAuthorityHeld(false, {})).toBe(false);
    expect(nextAuthorityHeld(true, null)).toBe(true);
    expect(nextAuthorityHeld(false, undefined)).toBe(false);
  });

  it('gates the autonomous path on BOTH the mode and the lease', () => {
    expect(autonomousActive(true, true)).toBe(true);   // enabled + held → autonomous
    expect(autonomousActive(true, false)).toBe(false);  // held lapsed → passive
    expect(autonomousActive(false, true)).toBe(false);  // monitor mode → passive
    expect(autonomousActive(false, false)).toBe(false);
  });
});

describe('FrameCoalescer', () => {
  const drainKinds = (fc: FrameCoalescer): string[] => {
    const out: string[] = [];
    for (let item = fc.next(); item; item = fc.next()) out.push(`${item.kind}:${item.text}`);
    return out;
  };

  it('coalesces consecutive text deltas into one message frame', () => {
    const fc = new FrameCoalescer();
    fc.pushText('hel');
    fc.pushText('lo ');
    fc.pushText('there');
    expect(fc.length).toBe(1);
    expect(fc.next()).toEqual({ kind: 'message', text: 'hello there' });
    expect(fc.isEmpty).toBe(true);
  });

  it('seals text before a tool/status frame so ordering is preserved', () => {
    const fc = new FrameCoalescer();
    fc.pushText('thinking...');
    fc.pushFrame('tool', '[tool: Bash] ./strategic');
    fc.pushText('after');
    // Three ordered items: the sealed text, the tool, then a NEW text frame.
    expect(fc.length).toBe(3);
    expect(drainKinds(fc)).toEqual([
      'message:thinking...',
      'tool:[tool: Bash] ./strategic',
      'message:after',
    ]);
  });

  it('queues a burst of tool frames as discrete items (drained one at a time)', () => {
    const fc = new FrameCoalescer();
    for (let i = 0; i < 30; i++) fc.pushFrame('tool', `t${i}`);
    // A burst of 30 tool_use blocks in one message → 30 queued items, NOT 30
    // immediate emits; the owner drains at most one per rate tick.
    expect(fc.length).toBe(30);
    expect(fc.next()).toEqual({ kind: 'tool', text: 't0' });
    expect(fc.length).toBe(29);
  });

  it('seal() closes the trailing open text so a later delta starts a fresh frame', () => {
    const fc = new FrameCoalescer();
    fc.pushText('turn-A tail');
    fc.seal(); // boundary flush seals the ending turn's open text
    fc.pushText('turn-B head');
    // Without the seal these would coalesce into ONE frame; sealed → two ordered
    // frames, so turn A's tail can never merge into turn B's first delta.
    expect(fc.length).toBe(2);
    expect(fc.next()).toEqual({ kind: 'message', text: 'turn-A tail' });
    expect(fc.next()).toEqual({ kind: 'message', text: 'turn-B head' });
  });

  it('seal() is a no-op on an empty or already-sealed queue', () => {
    const fc = new FrameCoalescer();
    fc.seal(); // empty
    expect(fc.isEmpty).toBe(true);
    fc.pushText('a');
    fc.pushFrame('tool', '[tool: Bash] x'); // seals the text as a side effect
    fc.seal(); // already sealed tail → no structural change
    expect(fc.length).toBe(2);
    expect(fc.next()).toEqual({ kind: 'message', text: 'a' });
    expect(fc.next()).toEqual({ kind: 'tool', text: '[tool: Bash] x' });
  });

  it('ignores empty text and drain() empties the queue in order', () => {
    const fc = new FrameCoalescer();
    fc.pushText('');
    expect(fc.isEmpty).toBe(true);
    fc.pushText('a');
    fc.pushFrame('status', '[strategic-session] hi');
    const drained = fc.drain();
    expect(drained).toEqual([
      { kind: 'message', text: 'a' },
      { kind: 'status', text: '[strategic-session] hi' },
    ]);
    expect(fc.isEmpty).toBe(true);
  });
});

describe('serializeLedger / restoreLedger', () => {
  const makeRecord = (surfaceKey: string, atTick: number): ExamRecord => ({
    surfaceKey,
    atTick,
    at: '2026-01-01T00:00:00Z',
    verdict: 'clear',
    note: 'ok',
  });

  it('round-trips a ledger through serialize → restore', () => {
    const records = [makeRecord('direction:d-1', 1), makeRecord('qualification', 2)];
    const serialized = serializeLedger(records, 2, false);
    const restored = restoreLedger(serialized);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.records).toHaveLength(2);
    expect(restored.records[0].surfaceKey).toBe('direction:d-1');
    expect(restored.records[1].surfaceKey).toBe('qualification');
    expect(restored.tick).toBe(2);
    expect(restored.corrective).toBe(false);
  });

  it('restoreLedger returns null for undefined / empty / malformed input', () => {
    expect(restoreLedger(undefined)).toBeNull();
    expect(restoreLedger(null)).toBeNull();
    expect(restoreLedger('')).toBeNull();
    expect(restoreLedger('not json{')).toBeNull();
    expect(restoreLedger('"just a string"')).toBeNull();
    expect(restoreLedger('[1,2,3]')).toBeNull();
  });

  it('caps records at EXAM_RECORDS_CAP on serialize', () => {
    const records = Array.from({ length: EXAM_RECORDS_CAP + 10 }, (_, i) => makeRecord(`code:dir${i}`, i));
    const serialized = serializeLedger(records, EXAM_RECORDS_CAP + 10, false);
    const restored = restoreLedger(serialized);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.records.length).toBeLessThanOrEqual(EXAM_RECORDS_CAP);
    // Should keep a suffix of the original records in order.
    expect(restored.records.map(r => r.surfaceKey)).toEqual(
      records.slice(-restored.records.length).map(r => r.surfaceKey),
    );
  });

  it('drops oldest records to stay within EXAM_LEDGER_MAX_BYTES', () => {
    // Build records with max-length notes so the total serialized size exceeds
    // the byte budget when all records are included.
    const bigNote = 'x'.repeat(EXAM_NOTE_CAP);
    const records = Array.from({ length: EXAM_RECORDS_CAP }, (_, i) => ({
      ...makeRecord(`code:dir${i}`, i),
      note: bigNote,
    }));
    const serialized = serializeLedger(records, EXAM_RECORDS_CAP, false);
    // Must fit within the byte budget.
    expect(Buffer.byteLength(serialized, 'utf8')).toBeLessThanOrEqual(EXAM_LEDGER_MAX_BYTES);
    // Must be valid JSON parseable by restoreLedger.
    const restored = restoreLedger(serialized);
    expect(restored).not.toBeNull();
    // Records must be the most recent ones (tail of the array).
    if (restored !== null && restored.records.length > 0) {
      const firstIdx = parseInt(restored.records[0].surfaceKey.replace('code:dir', ''), 10);
      const lastIdx = EXAM_RECORDS_CAP - 1;
      expect(firstIdx).toBeGreaterThan(0); // oldest dropped
      expect(restored.records[restored.records.length - 1].surfaceKey).toBe(`code:dir${lastIdx}`);
    }
  });

  it('truncates note fields at EXAM_NOTE_CAP on serialize', () => {
    const longNote = 'x'.repeat(EXAM_NOTE_CAP + 50);
    const records = [{ ...makeRecord('qualification', 1), note: longNote }];
    const serialized = serializeLedger(records, 1, false);
    const restored = restoreLedger(serialized);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.records[0].note).toHaveLength(EXAM_NOTE_CAP);
  });

  it('preserves corrective flag', () => {
    const serialized = serializeLedger([], 5, true);
    const restored = restoreLedger(serialized);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.corrective).toBe(true);
    expect(restored.tick).toBe(5);
    expect(restored.records).toHaveLength(0);
  });

  it('round-trips the operatorDecisionCorrective flag when set true', () => {
    const serialized = serializeLedger([], 7, false, true);
    const restored = restoreLedger(serialized);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.operatorDecisionCorrective).toBe(true);
    expect(restored.tick).toBe(7);
    // The two corrective flags are independent — corrective stays false here.
    expect(restored.corrective).toBe(false);
  });

  it('defaults operatorDecisionCorrective to false when the 4th arg is omitted', () => {
    const serialized = serializeLedger([makeRecord('qualification', 1)], 1, false);
    const restored = restoreLedger(serialized);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.operatorDecisionCorrective).toBe(false);
  });

  it('restores operatorDecisionCorrective as false from a legacy blob lacking the field', () => {
    // A blob written before this field existed (only records/tick/corrective).
    const legacy = JSON.stringify({ records: [], tick: 3, corrective: true });
    const restored = restoreLedger(legacy);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.operatorDecisionCorrective).toBe(false);
    expect(restored.corrective).toBe(true);
  });

  it('round-trips an interrupted verdict without coercing it to unevidenced', () => {
    const records: ExamRecord[] = [{
      surfaceKey: 'code:backend',
      atTick: 3,
      at: '2026-01-01T00:00:00Z',
      verdict: 'interrupted',
      note: 'turn interrupted before completion',
    }];
    const serialized = serializeLedger(records, 3, false);
    const restored = restoreLedger(serialized);
    if (restored === null) throw new Error('expected non-null restore');
    expect(restored.records).toHaveLength(1);
    expect(restored.records[0].verdict).toBe('interrupted');
    expect(restored.records[0].surfaceKey).toBe('code:backend');
  });
});

describe('refreshSurfaceCatalog', () => {
  const makeDir = (id: string): ExamSurface => ({
    key: `direction:${id}`,
    kind: 'direction',
    label: `direction ${id}`,
    hint: `./fetch direction ${id}`,
  });

  const makeFetch = (key: string): ExamSurface => ({
    key,
    kind: 'fetch',
    label: key,
    hint: `./fetch ${key}`,
  });

  it('preserves catalog when active_direction_ids matches exactly', () => {
    const catalog = [makeDir('d-1'), makeDir('d-2'), makeFetch('qualification')];
    const result = refreshSurfaceCatalog(catalog, ['d-1', 'd-2'], {});
    expect(result).toHaveLength(3);
    expect(result.map(s => s.key)).toEqual(['direction:d-1', 'direction:d-2', 'qualification']);
  });

  it('prunes retired directions', () => {
    const catalog = [makeDir('d-old'), makeDir('d-keep'), makeFetch('qualification')];
    const result = refreshSurfaceCatalog(catalog, ['d-keep'], {});
    expect(result.find(s => s.key === 'direction:d-old')).toBeUndefined();
    expect(result.find(s => s.key === 'direction:d-keep')).toBeDefined();
    expect(result.find(s => s.key === 'qualification')).toBeDefined();
  });

  it('adds new directions before non-direction surfaces', () => {
    const catalog = [makeDir('d-1'), makeFetch('qualification'), makeFetch('version-phase')];
    const result = refreshSurfaceCatalog(catalog, ['d-1', 'd-new'], {
      directions_summary: [{ id: 'd-new', text: 'A new direction' }],
    });
    const keys = result.map(s => s.key);
    // d-new inserted before fetch surfaces, d-1 stays first
    expect(keys[0]).toBe('direction:d-1');
    expect(keys[1]).toBe('direction:d-new');
    expect(keys[2]).toBe('qualification');
    expect(keys[3]).toBe('version-phase');
  });

  it('uses payload directions_summary for label enrichment', () => {
    const catalog = [makeFetch('qualification')];
    const result = refreshSurfaceCatalog(catalog, ['d-123'], {
      directions_summary: [{ id: 'd-123', text: 'Improve test coverage' }],
    });
    const dir = result.find(s => s.key === 'direction:d-123');
    if (dir === undefined) throw new Error('expected direction:d-123 in catalog');
    expect(dir.label).toContain('Improve test coverage');
  });

  it('returns catalog with no direction surfaces when active list is empty', () => {
    const catalog = [makeFetch('qualification')];
    const result = refreshSurfaceCatalog(catalog, [], {});
    expect(result).toHaveLength(1);
    expect(result[0].key).toBe('qualification');
  });

  it('appends to end when catalog has no non-direction surfaces', () => {
    const catalog = [makeDir('d-1')];
    const result = refreshSurfaceCatalog(catalog, ['d-1', 'd-2'], {});
    expect(result.map(s => s.key)).toEqual(['direction:d-1', 'direction:d-2']);
  });
});

// ── Compaction helpers ─────────────────────────────────────────────────────

describe('modelContextLimit', () => {
  it('returns 200K for opus models', () => {
    expect(modelContextLimit('claude-opus-4-8')).toBe(200_000);
    expect(modelContextLimit('claude-opus-4-7')).toBe(200_000);
    expect(modelContextLimit('claude-opus-4')).toBe(200_000);
  });

  it('returns 200K for sonnet models', () => {
    expect(modelContextLimit('claude-sonnet-5')).toBe(200_000);
    expect(modelContextLimit('claude-sonnet-4-5')).toBe(200_000);
    expect(modelContextLimit('sonnet')).toBe(200_000);
  });

  it('returns 128K for older haiku models', () => {
    expect(modelContextLimit('claude-haiku-3-5')).toBe(128_000);
    expect(modelContextLimit('haiku')).toBe(128_000);
  });

  it('returns 200K for haiku-4-5', () => {
    expect(modelContextLimit('claude-haiku-4-5')).toBe(200_000);
    expect(modelContextLimit('haiku-4-5')).toBe(200_000);
  });

  it('returns 200K for fable models', () => {
    expect(modelContextLimit('claude-fable-5')).toBe(200_000);
    expect(modelContextLimit('fable')).toBe(200_000);
  });

  it('returns DEFAULT_CONTEXT_LIMIT for unrecognized models', () => {
    expect(modelContextLimit('unknown-model')).toBe(DEFAULT_CONTEXT_LIMIT);
    expect(modelContextLimit('')).toBe(DEFAULT_CONTEXT_LIMIT);
    expect(modelContextLimit()).toBe(DEFAULT_CONTEXT_LIMIT);
  });

  it('matches case-insensitively', () => {
    expect(modelContextLimit('CLAUDE-OPUS-4-8')).toBe(200_000);
    expect(modelContextLimit('Claude-Sonnet-5')).toBe(200_000);
  });
});

describe('compactionThreshold', () => {
  it('returns 80% of model limit by default', () => {
    // 80% of 200K = 160K
    expect(compactionThreshold('claude-opus-4-8')).toBe(160_000);
    // 80% of 128K = 102400
    expect(compactionThreshold('claude-haiku-3-5')).toBe(Math.floor(128_000 * COMPACTION_THRESHOLD_RATIO));
  });

  it('uses explicit override when provided', () => {
    expect(compactionThreshold('claude-opus-4-8', 50_000)).toBe(50_000);
  });

  it('returns Infinity when explicit threshold is 0 (disabled)', () => {
    expect(compactionThreshold('claude-opus-4-8', 0)).toBe(Number.POSITIVE_INFINITY);
  });

  it('falls back to DEFAULT_CONTEXT_LIMIT for unknown models', () => {
    expect(compactionThreshold('unknown')).toBe(Math.floor(DEFAULT_CONTEXT_LIMIT * COMPACTION_THRESHOLD_RATIO));
  });
});

describe('buildCompactionCheckpoint', () => {
  const fullCtx: CompactionCheckpointContext = {
    roleIdentity: 'You are the resident strategist for TestProject.',
    examLedgerJson: JSON.stringify({ records: [{ surfaceKey: 'direction:d-1', atTick: 5, at: '2026-01-01T00:00:00Z', verdict: 'found', note: 'needs follow-up' }], tick: 5, corrective: false }),
    recentInteractions: 'Recent: examined direction:d-1 — found actionable lead.',
    autonomousState: 'Examination tick: 5\nFocus rotation: direction:d-1, qualification',
  };

  it('includes all sections in the checkpoint', () => {
    const result = buildCompactionCheckpoint(fullCtx);
    expect(result).toContain('Session checkpoint');
    expect(result).toContain('compaction restart');
    expect(result).toContain('You are the resident strategist for TestProject');
    expect(result).toContain('Examination ledger (carried forward)');
    expect(result).toContain('direction:d-1');
    expect(result).toContain('Recent context (carried forward)');
    expect(result).toContain('examined direction:d-1');
    expect(result).toContain('Current state');
    expect(result).toContain('Examination tick: 5');
  });

  it('omits sections with empty content', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role only',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
    });
    expect(result).toContain('role only');
    expect(result).not.toContain('Examination ledger');
    expect(result).not.toContain('Recent context');
    expect(result).not.toContain('Current state');
  });

  it('produces a non-empty string for minimal context', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'minimal role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
    });
    expect(result.length).toBeGreaterThan(0);
    expect(result).toContain('minimal role');
    expect(result).toContain('Session checkpoint');
  });

  it('includes a resume instruction at the end', () => {
    const result = buildCompactionCheckpoint(fullCtx);
    expect(result).toContain('Resume from this checkpoint');
  });

  it('buildCompactionCheckpoint carries durable self-model grounding', () => {
    const ctx: CompactionCheckpointContext = {
      roleIdentity: 'You are the resident strategist for Acme.',
      examLedgerJson: JSON.stringify({ records: [], tick: 0, corrective: false }),
      recentInteractions: '',
      autonomousState: 'Examination tick: 0',
      problemModelJson: JSON.stringify({ goals: ['improve testing'] }),
      visionJson: JSON.stringify({ future: 'testable code' }),
      strategicMemoryNotesText: '- Prefer concrete action over abstract discussion\n- Verify findings before escalating',
      openMessagesText: 'msg: improve test coverage',
      priorSessionContextText: 'Prior session explored auth flows.',
      directionsText: '- d-1 (active) — improve test coverage',
      runningUpdate: 'Coverage improved by 12%',
    };
    const result = buildCompactionCheckpoint(ctx);

    // The self-model grounding section appears as a top-level section.
    expect(result).toContain('## Self-model grounding');

    // Every grounding field that was present renders as a sub-section.
    expect(result).toContain('### Problem model');
    expect(result).toContain('improve testing');
    expect(result).toContain('### Vision');
    expect(result).toContain('testable code');
    expect(result).toContain('### Standing behavioral lessons');
    expect(result).toContain('Prefer concrete action');
    expect(result).toContain('### Open operator messages');
    expect(result).toContain('improve test coverage');
    expect(result).toContain('### Prior session context');
    expect(result).toContain('explored auth flows');
    expect(result).toContain('### Active directions');
    expect(result).toContain('d-1 (active)');
    expect(result).toContain('### Running update');
    expect(result).toContain('12%');

    // Existing sections (role identity, ledger, interactions, state) are untouched.
    expect(result).toContain('You are the resident strategist for Acme');
    expect(result).toContain('Examination ledger (carried forward)');
    expect(result).toContain('Examination tick: 0');
  });

  it('buildCompactionCheckpoint renders the recently-answered conversation as a read-only section', () => {
    const ctx: CompactionCheckpointContext = {
      roleIdentity: 'You are the resident strategist for Acme.',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      recentHandledConversationText: '- [FEEDBACK] (id: h1, from: user, answered) drop the caching idea',
    };
    const result = buildCompactionCheckpoint(ctx);
    expect(result).toContain('## Recently answered operator conversation');
    expect(result).toContain('read-only — already handled, do NOT re-answer');
    expect(result).toContain('drop the caching idea');
    expect(result).toContain('do not re-propose it');
  });

  it('buildCompactionCheckpoint omits the recently-answered conversation section when blank', () => {
    const ctx: CompactionCheckpointContext = {
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      recentHandledConversationText: '   ',
    };
    expect(buildCompactionCheckpoint(ctx)).not.toContain('Recently answered operator conversation');
  });

  it('test_checkpoint_grounding_marked_snapshot_not_current', () => {
    // Grounding IS populated — verify it is marked as a session-start snapshot
    // and includes a directive to ./fetch current state rather than rely on stale data.
    const ctx: CompactionCheckpointContext = {
      roleIdentity: 'You are the resident strategist for Test.',
      examLedgerJson: JSON.stringify({ records: [], tick: 0, corrective: false }),
      recentInteractions: '',
      autonomousState: 'Examination tick: 0',
      problemModelJson: JSON.stringify({ goals: ['improve testing'] }),
      directionsText: '- d-1 active',
      visionJson: JSON.stringify({ future: 'testable code' }),
      openMessagesText: 'msg: improve test coverage',
    };
    const result = buildCompactionCheckpoint(ctx);

    // Grounding section header contains "(session-start snapshot)" marker.
    expect(result).toContain('## Self-model grounding (session-start snapshot)');
    // The "Snapshot note" preamble warns the data may be stale.
    expect(result).toContain('Snapshot note');
    expect(result).toMatch(/may not reflect/);
    // ./fetch directives let the model recover current state.
    expect(result).toContain('./fetch problem-model');
    expect(result).toContain('./fetch direction <id>');
    expect(result).toContain('./fetch message');
    // The top-level header must not claim the model 'retains ... all durable state'.
    expect(result).not.toMatch(/retain.*all durable state/);

    // WITHOUT grounding fields — the snapshot/stale markers MUST NOT appear.
    const minimalCtx: CompactionCheckpointContext = {
      roleIdentity: 'role only',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
    };
    const minimalResult = buildCompactionCheckpoint(minimalCtx);
    expect(minimalResult).not.toContain('(session-start snapshot)');
    expect(minimalResult).not.toContain('Snapshot note');
    expect(minimalResult).not.toContain('may not reflect');
  });

  it('omits self-model grounding when no fields are populated', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role only',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
    });
    expect(result).not.toContain('## Self-model grounding');
    expect(result).not.toContain('### Problem model');
  });

  it('test_compaction_checkpoint_carries_retired_staged_prior_and_field_level_grounding_fallback', () => {
    // Acceptance test for the two compaction-checkpoint grounding-fidelity fixes.
    //
    // PROBLEM 1 — the checkpoint must carry the three STATIC session-start
    // grounding sections the bootstrap renders (retired / staged / prior-cycle
    // decisions) so a post-compaction session does not re-derive settled facts
    // or re-propose already-retired/staged directions. They render when
    // non-empty and are omitted when blank.
    const retiredDirectionsText = 'These directions were recently retired.\n- [retired] old auth work (id: d-9)';
    const stagedDirectionsText = 'These directions are already staged for a next version.\n- staged infra work (id: d-7)';
    const previousDecisionOutcomesText = 'cycle=c-1\nCreated directions:\n- created d-3 | status=active';

    const withStatic = buildCompactionCheckpoint({
      roleIdentity: 'You are the resident strategist for Acme.',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      retiredDirectionsText,
      stagedDirectionsText,
      previousDecisionOutcomesText,
    });
    // Section headers mirror the bootstrap wording exactly.
    expect(withStatic).toContain('## Already-staged (next-version) directions');
    expect(withStatic).toContain('staged infra work (id: d-7)');
    expect(withStatic).toContain('## Recently retired / completed directions');
    expect(withStatic).toContain('old auth work (id: d-9)');
    expect(withStatic).toContain('## Prior-cycle decision outcomes');
    expect(withStatic).toContain('created d-3');

    // Blank static texts → none of the three headers appear.
    const withoutStatic = buildCompactionCheckpoint({
      roleIdentity: 'role only',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      retiredDirectionsText: '',
      stagedDirectionsText: '   ',
      previousDecisionOutcomesText: undefined,
    });
    expect(withoutStatic).not.toContain('## Already-staged (next-version) directions');
    expect(withoutStatic).not.toContain('## Recently retired / completed directions');
    expect(withoutStatic).not.toContain('## Prior-cycle decision outcomes');

    // PROBLEM 2 — field-level grounding fallback. A successful refetch that
    // returns an empty STRUCTURAL field (problemModelJson / visionJson) is a
    // transient degraded read: fall back to the snapshot rather than dropping
    // the section. An empty refreshed openMessagesText still WINS (messages the
    // operator's turn just handled must disappear).
    const snapshot: CheckpointGrounding = {
      problemModelJson: JSON.stringify({ goals: ['SNAPSHOT goal'] }, null, 2),
      visionJson: JSON.stringify({ future: 'SNAPSHOT vision' }, null, 2),
      strategicMemoryNotesText: '- SNAPSHOT lesson',
      runningUpdate: 'SNAPSHOT running update',
      openMessagesText: 'SNAPSHOT open message',
    };
    const refreshedDegraded: CheckpointGrounding = {
      problemModelJson: '',
      visionJson: '',
      strategicMemoryNotesText: '',
      runningUpdate: '',
      openMessagesText: '',
    };
    const resolved = resolveCheckpointGrounding(snapshot, refreshedDegraded);
    // Structural fields fall back to the snapshot on an empty refetch.
    expect(resolved.problemModelJson).toContain('SNAPSHOT goal');
    expect(resolved.visionJson).toContain('SNAPSHOT vision');
    expect(resolved.strategicMemoryNotesText).toBe('- SNAPSHOT lesson');
    expect(resolved.runningUpdate).toBe('SNAPSHOT running update');
    // openMessagesText is the deliberate exception — empty refreshed wins.
    expect(resolved.openMessagesText).toBeUndefined();
  });

  it('buildCompactionCheckpoint carries contextPrefix when provided', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      contextPrefix: 'Operator Purpose: build a great product.\nConstraints: do not break prod.',
    });
    expect(result).toContain('Operator Purpose: build a great product.');
    expect(result).toContain('Constraints: do not break prod.');
  });

  it('buildCompactionCheckpoint carries skillsText as a ## Project Skills section', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      skillsText: '- **deploy** (`skill-deploy`) — `./deploy`\n  Deploy the application.',
    });
    expect(result).toContain('## Project Skills');
    expect(result).toContain('skill-deploy');
  });

  it('buildCompactionCheckpoint carries toolContentText as a ## Project Tool Content section', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      toolContentText: '### Notes (notes)\nKey insight: ship fast.',
    });
    expect(result).toContain('## Project Tool Content');
    expect(result).toContain('Key insight: ship fast.');
  });

  it('buildCompactionCheckpoint omits contextPrefix/skills/tools sections when blank or absent', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role only',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      contextPrefix: '',
      skillsText: '   ',
      toolContentText: undefined,
    });
    expect(result).not.toContain('## Project Skills');
    expect(result).not.toContain('## Project Tool Content');
  });
});

describe('formatStrategicMemoryNotes', () => {
  it('renders "- [set_at] note" per well-formed entry', () => {
    const out = formatStrategicMemoryNotes([
      { note: 'prefer concrete action', set_at: '2026-07-01T00:00:00Z' },
      { note: 'verify before escalating', set_at: '2026-07-02T00:00:00Z' },
    ]);
    expect(out).toBe(
      '- [2026-07-01T00:00:00Z] prefer concrete action\n' +
      '- [2026-07-02T00:00:00Z] verify before escalating',
    );
  });

  it('omits the timestamp when set_at is absent', () => {
    expect(formatStrategicMemoryNotes([{ note: 'no timestamp' }])).toBe('- no timestamp');
  });

  it('skips entries with a missing/blank note and drops non-object entries', () => {
    const out = formatStrategicMemoryNotes([
      { note: '   ' },
      { set_at: '2026-07-01T00:00:00Z' },
      'garbage',
      null,
      { note: 'kept' },
    ]);
    expect(out).toBe('- kept');
  });

  it('degrades to empty string for empty / non-array input', () => {
    expect(formatStrategicMemoryNotes([])).toBe('');
    expect(formatStrategicMemoryNotes(undefined)).toBe('');
    expect(formatStrategicMemoryNotes('nope')).toBe('');
  });
});

describe('formatCheckpointState', () => {
  it('maps a populated response into pretty-printed JSON + formatted notes + string', () => {
    const resp: CheckpointStateResponse = {
      problem_model: { goals: ['ship refresh'] },
      vision: { future: 'coherent strategist' },
      strategic_memory_notes: [{ note: 'do not re-derive', set_at: '2026-07-01T00:00:00Z' }],
      running_update: 'refresh landed',
    };
    const g = formatCheckpointState(resp);
    expect(g.problemModelJson).toBe(JSON.stringify({ goals: ['ship refresh'] }, null, 2));
    expect(g.visionJson).toBe(JSON.stringify({ future: 'coherent strategist' }, null, 2));
    expect(g.strategicMemoryNotesText).toBe('- [2026-07-01T00:00:00Z] do not re-derive');
    expect(g.runningUpdate).toBe('refresh landed');
  });

  it('coerces empty {} / missing / mistyped fields to empty strings (no throw)', () => {
    const g = formatCheckpointState({
      problem_model: {},
      vision: undefined,
      strategic_memory_notes: 'not-an-array',
      running_update: 42,
    });
    expect(g.problemModelJson).toBe('');
    expect(g.visionJson).toBe('');
    expect(g.strategicMemoryNotesText).toBe('');
    expect(g.runningUpdate).toBe('');
    // A fully empty response object must not throw.
    expect(() => formatCheckpointState({})).not.toThrow();
  });
});

describe('resolveCheckpointGrounding', () => {
  const snapshot: CheckpointGrounding = {
    problemModelJson: JSON.stringify({ goals: ['STALE goal'] }, null, 2),
    visionJson: JSON.stringify({ future: 'STALE vision' }, null, 2),
    strategicMemoryNotesText: '- STALE lesson',
    runningUpdate: 'STALE running update',
  };

  it('refreshed checkpoint state overrides session-start snapshot', () => {
    // The refreshed grounding is derived from a checkpoint-state response the
    // operator updated mid-session; every field must win over the snapshot.
    const refreshed = formatCheckpointState({
      problem_model: { goals: ['FRESH goal'] },
      vision: { future: 'FRESH vision' },
      strategic_memory_notes: [{ note: 'FRESH lesson', set_at: '2026-07-11T00:00:00Z' }],
      running_update: 'FRESH running update',
    });
    const resolved = resolveCheckpointGrounding(snapshot, refreshed);
    expect(resolved.problemModelJson).toContain('FRESH goal');
    expect(resolved.problemModelJson).not.toContain('STALE');
    expect(resolved.visionJson).toContain('FRESH vision');
    expect(resolved.visionJson).not.toContain('STALE');
    expect(resolved.strategicMemoryNotesText).toBe('- [2026-07-11T00:00:00Z] FRESH lesson');
    expect(resolved.runningUpdate).toBe('FRESH running update');

    // And end-to-end: the rendered checkpoint carries the fresh values, marked
    // as refreshed rather than a session-start snapshot.
    const checkpoint = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      ...resolved,
      groundingRefreshed: true,
    });
    expect(checkpoint).toContain('FRESH goal');
    expect(checkpoint).not.toContain('STALE');
    expect(checkpoint).toContain('## Self-model grounding (refreshed at compaction)');
    expect(checkpoint).not.toContain('(session-start snapshot)');
  });

  it('falls back to the snapshot when no refresh occurred (refreshed null/undefined)', () => {
    expect(resolveCheckpointGrounding(snapshot, null)).toEqual({
      problemModelJson: snapshot.problemModelJson,
      visionJson: snapshot.visionJson,
      strategicMemoryNotesText: snapshot.strategicMemoryNotesText,
      runningUpdate: snapshot.runningUpdate,
    });
    expect(resolveCheckpointGrounding(snapshot, undefined).problemModelJson)
      .toContain('STALE goal');
  });

  it('an empty refreshed STRUCTURAL field falls back to the snapshot (transient degraded read)', () => {
    // A successful refetch that returns empty problem-model/vision/memory/running
    // fields is treated as a transient degraded server read — NOT a mid-session
    // clear — so the checkpoint must fall back to the session-start snapshot
    // field-by-field rather than dropping structural grounding entirely. Only a
    // non-empty refreshed value overrides. (openMessagesText is the exception,
    // covered in the 'resolveCheckpointGrounding open_messages' suite.)
    const refreshed: CheckpointGrounding = {
      problemModelJson: JSON.stringify({ goals: ['FRESH goal'] }, null, 2),
      visionJson: '',
      strategicMemoryNotesText: '',
      runningUpdate: '',
    };
    const resolved = resolveCheckpointGrounding(snapshot, refreshed);
    // Non-empty refreshed value still wins.
    expect(resolved.problemModelJson).toContain('FRESH goal');
    expect(resolved.problemModelJson).not.toContain('STALE');
    // Empty refreshed structural fields fall back to the snapshot, not undefined.
    expect(resolved.visionJson).toBe(snapshot.visionJson);
    expect(resolved.visionJson).toContain('STALE vision');
    expect(resolved.strategicMemoryNotesText).toBe(snapshot.strategicMemoryNotesText);
    expect(resolved.runningUpdate).toBe(snapshot.runningUpdate);
  });
});

describe('formatCheckpointState open_messages field', () => {
  it('sets openMessagesText to empty string when open_messages is missing', () => {
    const g = formatCheckpointState({ problem_model: { k: 'v' } });
    expect(g.openMessagesText).toBe('');
  });

  it('sets openMessagesText to empty string when open_messages is an empty array', () => {
    const g = formatCheckpointState({ open_messages: [] });
    expect(g.openMessagesText).toBe('');
  });

  it('sets openMessagesText to empty string even when open_messages has entries (caller fills it)', () => {
    // openMessagesText is always '' from formatCheckpointState; the handler
    // overrides it with buildOpenMessagesText after the call.
    const g = formatCheckpointState({
      open_messages: [{ id: 'msg-1', content: 'hello', type: 'QUESTION', from: 'user' }],
    });
    expect(g.openMessagesText).toBe('');
  });
});

describe('resolveCheckpointGrounding open_messages', () => {
  const snapshot: CheckpointGrounding = {
    problemModelJson: '{}',
    visionJson: '{}',
    strategicMemoryNotesText: '',
    runningUpdate: '',
    openMessagesText: 'STALE open message',
  };

  it('refreshed openMessagesText wins over the snapshot', () => {
    const refreshed: CheckpointGrounding = {
      problemModelJson: '{}',
      visionJson: '{}',
      strategicMemoryNotesText: '',
      runningUpdate: '',
      openMessagesText: 'FRESH open message',
    };
    const resolved = resolveCheckpointGrounding(snapshot, refreshed);
    expect(resolved.openMessagesText).toBe('FRESH open message');
  });

  it('empty refreshed openMessagesText collapses to undefined (resolved messages win over stale snapshot)', () => {
    // The operator resolved all messages mid-session; the refreshed empty feed
    // must WIN so the checkpoint omits the section rather than re-showing stale text.
    const refreshed: CheckpointGrounding = {
      problemModelJson: '{}',
      visionJson: '{}',
      strategicMemoryNotesText: '',
      runningUpdate: '',
      openMessagesText: '',
    };
    const resolved = resolveCheckpointGrounding(snapshot, refreshed);
    expect(resolved.openMessagesText).toBeUndefined();
  });

  it('falls back to the snapshot openMessagesText when refresh is null', () => {
    const resolved = resolveCheckpointGrounding(snapshot, null);
    expect(resolved.openMessagesText).toBe('STALE open message');
  });

  it('snapshot with no openMessagesText falls back to undefined when refresh is null', () => {
    const snapshotNoMsgs: CheckpointGrounding = {
      problemModelJson: '{}',
      visionJson: '{}',
      strategicMemoryNotesText: '',
      runningUpdate: '',
    };
    const resolved = resolveCheckpointGrounding(snapshotNoMsgs, null);
    expect(resolved.openMessagesText).toBeUndefined();
  });
});

describe('formatCheckpointState recent_handled_conversation field', () => {
  it('sets recentHandledConversationText to empty string regardless of input (caller fills it)', () => {
    // Like openMessagesText, formatCheckpointState always leaves this '' and the
    // handler overrides it with formatRecentHandledConversation after the call.
    expect(formatCheckpointState({ problem_model: { k: 'v' } }).recentHandledConversationText).toBe('');
    expect(formatCheckpointState({ recent_handled_conversation: [] }).recentHandledConversationText).toBe('');
    expect(
      formatCheckpointState({
        recent_handled_conversation: [{ id: 'h1', content: 'x', type: 'NOTE', from: 'user' }],
      }).recentHandledConversationText,
    ).toBe('');
  });
});

describe('resolveCheckpointGrounding recent_handled_conversation', () => {
  const snapshot: CheckpointGrounding = {
    problemModelJson: '{}',
    visionJson: '{}',
    strategicMemoryNotesText: '',
    runningUpdate: '',
    recentHandledConversationText: 'STALE handled tail',
  };

  it('refreshed recentHandledConversationText wins over the snapshot', () => {
    const refreshed: CheckpointGrounding = {
      problemModelJson: '{}',
      visionJson: '{}',
      strategicMemoryNotesText: '',
      runningUpdate: '',
      recentHandledConversationText: 'FRESH handled tail',
    };
    const resolved = resolveCheckpointGrounding(snapshot, refreshed);
    expect(resolved.recentHandledConversationText).toBe('FRESH handled tail');
  });

  it('empty refreshed recentHandledConversationText collapses to undefined (fresh wins over stale)', () => {
    const refreshed: CheckpointGrounding = {
      problemModelJson: '{}',
      visionJson: '{}',
      strategicMemoryNotesText: '',
      runningUpdate: '',
      recentHandledConversationText: '',
    };
    const resolved = resolveCheckpointGrounding(snapshot, refreshed);
    expect(resolved.recentHandledConversationText).toBeUndefined();
  });

  it('falls back to the snapshot recentHandledConversationText when refresh is null', () => {
    const resolved = resolveCheckpointGrounding(snapshot, null);
    expect(resolved.recentHandledConversationText).toBe('STALE handled tail');
  });
});

describe('buildCompactionCheckpoint groundingRefreshed note', () => {
  it('uses the refreshed-at-compaction note when groundingRefreshed is true', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      problemModelJson: JSON.stringify({ goals: ['g'] }),
      groundingRefreshed: true,
    });
    expect(result).toContain('## Self-model grounding (refreshed at compaction)');
    expect(result).toContain('Refreshed note');
    expect(result).not.toContain('(session-start snapshot)');
    expect(result).not.toContain('Snapshot note');
  });

  it('defaults to the session-start snapshot note when groundingRefreshed is falsy', () => {
    const result = buildCompactionCheckpoint({
      roleIdentity: 'role',
      examLedgerJson: '',
      recentInteractions: '',
      autonomousState: '',
      problemModelJson: JSON.stringify({ goals: ['g'] }),
    });
    expect(result).toContain('## Self-model grounding (session-start snapshot)');
    expect(result).not.toContain('(refreshed at compaction)');
  });
});

describe('residentConfig compaction knobs', () => {
  it('reads explicit compaction threshold from payload', () => {
    const c = residentConfig({ strategic_session_compaction_threshold_tokens: 50_000 });
    expect(c.compactionThresholdTokens).toBe(50_000);
  });

  it('treats zero explicit threshold as disabled (0)', () => {
    const c = residentConfig({ strategic_session_compaction_threshold_tokens: 0 });
    expect(c.compactionThresholdTokens).toBe(0);
  });

  it('leaves threshold undefined when payload omits it', () => {
    const c = residentConfig({});
    expect(c.compactionThresholdTokens).toBeUndefined();
  });

  it('reads compaction cooldown from payload', () => {
    const c = residentConfig({ strategic_session_compaction_cooldown_seconds: 120 });
    expect(c.compactionCooldownMs).toBe(120_000);
  });

  it('clamps compaction cooldown to minimum 60s', () => {
    const c = residentConfig({ strategic_session_compaction_cooldown_seconds: 10 });
    expect(c.compactionCooldownMs).toBe(60_000);
  });

  describe('serializeDecisionsRegister / restoreDecisionsRegister', () => {
    const makeEntry = (surfaceKey: string, atTick: number, note?: string): DecisionsRegisterEntry => ({
      atTick,
      at: '2026-07-10T12:00:00Z',
      surfaceKey,
      verdict: 'clear',
      note: note ?? 'ok',
    });

    it('round-trips a register through serialize → restore', () => {
      const reg = [makeEntry('direction:d-1', 1), makeEntry('qualification', 2)];
      const serialized = serializeDecisionsReg(reg);
      const restored = restoreDecisionsReg(serialized);
      expect(restored).not.toBeNull();
      const r = restored as DecisionsRegisterEntry[];
      expect(r).toHaveLength(2);
      expect(r[0].surfaceKey).toBe('direction:d-1');
      expect(r[1].surfaceKey).toBe('qualification');
      expect(r[0].atTick).toBe(1);
      expect(r[1].verdict).toBe('clear');
    });

    it('restoreDecisionsReg returns null for undefined / empty / malformed input', () => {
      expect(restoreDecisionsReg(undefined)).toBeNull();
      expect(restoreDecisionsReg(null)).toBeNull();
      expect(restoreDecisionsReg('')).toBeNull();
      expect(restoreDecisionsReg('not json{')).toBeNull();
      expect(restoreDecisionsReg('"just a string"')).toBeNull();
      expect(restoreDecisionsReg('{}')).toEqual([]);
    });

    it('empty register returns a valid empty entries array from serializeDecisionsReg', () => {
      const serialized = serializeDecisionsReg([]);
      const parsed = JSON.parse(serialized) as { entries: DecisionsRegisterEntry[] };
      expect(parsed.entries).toEqual([]);
    });

    it('caps at DECISIONS_REGISTER_CAP entries on restore', () => {
      const many = Array.from({ length: 60 }, (_, i) => makeEntry(`surface-${i}`, i));
      const serialized = serializeDecisionsReg(many);
      const restored = restoreDecisionsReg(serialized);
      expect(restored).not.toBeNull();
      const r = restored as DecisionsRegisterEntry[];
      expect(r.length).toBeLessThanOrEqual(50);
      // Should keep the most recent entries (suffix of the original).
      expect(r[r.length - 1].surfaceKey).toBe('surface-59');
    });
  });
});

describe('formatDirectionGroundingText', () => {
  it('returns empty string for empty or null directions', () => {
    expect(formatDirectionGroundingText([])).toBe('');
    expect(formatDirectionGroundingText(null)).toBe('');
    expect(formatDirectionGroundingText(undefined)).toBe('');
  });

  it('formats a single active direction with id and text', () => {
    const result = formatDirectionGroundingText([
      { id: 'dir-1', text: 'Improve onboarding flow', status: 'active' },
    ]);
    expect(result).toMatch(/refreshed from heartbeat/);
    expect(result).toContain('./fetch direction dir-1');
    expect(result).toContain('Improve onboarding flow');
    expect(result).not.toContain('[ACTIVE]');
  });

  it('appends status tag for non-active directions', () => {
    const result = formatDirectionGroundingText([
      { id: 'dir-2', text: 'Deprecated initiative', status: 'completed' },
    ]);
    expect(result).toContain('[COMPLETED]');
  });

  it('handles missing text gracefully', () => {
    const result = formatDirectionGroundingText([
      { id: 'dir-3', text: '', status: 'active' },
    ]);
    expect(result).toContain('./fetch direction dir-3');
    expect(result).not.toContain('—');
  });

  it('renders multiple directions separated by newlines', () => {
    const result = formatDirectionGroundingText([
      { id: 'a', text: 'One', status: 'active' },
      { id: 'b', text: 'Two', status: 'paused' },
      { id: 'c', text: 'Three', status: 'active' },
    ]);
    const lines = result.split('\n');
    expect(lines[0]).toBe('(refreshed from heartbeat)');
    expect(lines).toHaveLength(4); // header + 3 entries
    expect(lines[1]).toContain('./fetch direction a');
    expect(lines[2]).toContain('./fetch direction b');
    expect(lines[2]).toContain('[PAUSED]');
    expect(lines[3]).toContain('./fetch direction c');
  });

  it('truncates text beyond 120 characters', () => {
    const longText = 'a'.repeat(200);
    const result = formatDirectionGroundingText([
      { id: 'dir-long', text: longText, status: 'active' },
    ]);
    expect(result).toContain('a'.repeat(120));
    expect(result).not.toContain('a'.repeat(121));
  });
});
