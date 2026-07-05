import { describe, it, expect } from 'vitest';
import {
  residentConfig, initialCursor, advanceCursorFromMessages,
  BoundedSeenSet, dedupNewMessages, extractMessageContent,
  combineOperatorMessages, buildBootstrapPrompt, buildAutonomousPrompt,
  resolveTerminalStatus, nextAuthorityHeld, autonomousActive, FrameCoalescer,
  buildPriorDecisionsText,
  buildRetiredContextText,
  buildStagedDirectionsText,
  MESSAGE_POLL_LIMIT,
  buildSurfaceCatalog, nextFocus, parseTickVerdict, parseAgendaContinue,
  openFoundLeads, classifyTickOutcome, shouldRunAutonomousTurn,
  AUTONOMOUS_FLOOR_TICK_MS,
  type ExamSurface, type ExamRecord,
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
  });

  it('reads the payload knobs (seconds → ms)', () => {
    const c = residentConfig({
      strategic_session_idle_tick_seconds: 3,
      strategic_session_message_poll_seconds: 1,
      strategic_session_turn_timeout_seconds: 90,
      strategic_session_min_tick_seconds: 120,
      strategic_session_autonomous_turn_timeout_seconds: 180,
      strategic_session_max_ops_per_turn: 5,
    });
    expect(c.idleTickMs).toBe(3000);
    expect(c.messagePollMs).toBe(1000);
    expect(c.turnTimeoutMs).toBe(90_000);
    expect(c.minTickMs).toBe(120_000);
    expect(c.autonomousTurnTimeoutMs).toBe(180_000);
    expect(c.maxOpsPerTurn).toBe(5);
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

describe('extractMessageContent / combineOperatorMessages', () => {
  it('extracts and trims content', () => {
    expect(extractMessageContent({ content: '  hello  ' })).toBe('hello');
    expect(extractMessageContent({})).toBe('');
  });

  it('returns a single message verbatim', () => {
    expect(combineOperatorMessages([{ content: 'just one' }])).toBe('just one');
  });

  it('batches multiple messages with headers', () => {
    const out = combineOperatorMessages([{ content: 'first' }, { content: 'second' }]);
    expect(out).toContain('[operator message 1/2]');
    expect(out).toContain('first');
    expect(out).toContain('[operator message 2/2]');
    expect(out).toContain('second');
  });

  it('skips empty messages', () => {
    expect(combineOperatorMessages([{ content: '' }, { content: '   ' }])).toBe('');
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
});

describe('parseAgendaContinue', () => {
  it('detects a literal AGENDA-CONTINUE line', () => {
    expect(parseAgendaContinue('did work\nAGENDA-CONTINUE\nTICK-VERDICT: ADVANCED — x')).toBe(true);
    expect(parseAgendaContinue('no more to do here')).toBe(false);
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
