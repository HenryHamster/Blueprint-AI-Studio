import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicStreamingBubble, StrategicActivityStrip, StrategicResidentActivityWidget } from './StrategicLiveTurn';
import type {
  StrategicActivityItem,
  StrategicTurnSegment,
  ResidentActivityState,
} from '../utils/strategicSessionLive';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';

// Worst case for the streaming preview bubble: a single unbroken token (a long
// URL / file path / stack frame — common in strategist replies) with no spaces
// that must WRAP rather than stretch the conversation column past the viewport.
const LONG_UNBROKEN_TEXT =
  'https://github.com/HenryHamster/BlueprintAIStudio/blob/main/backend/app/' +
  'a'.repeat(300) +
  '/very_long_module_path_with_no_spaces_that_would_otherwise_overflow.py#L1234567890';

// A turn carrying BOTH a reasoning run and a reply run — the reasoning must render
// in its own muted, set-apart block above the primary reply and neither may overflow.
const REASONING_PLUS_REPLY_SEGMENTS: StrategicTurnSegment[] = [
  {
    kind: 'reasoning',
    text: `Weighing the trade-offs: ${'w'.repeat(300)} versus the cheaper path.`,
  },
  { kind: 'reply', text: LONG_UNBROKEN_TEXT },
];

// Worst case for the ephemeral activity strip: activity one-liners whose `.text`
// is a long unbroken string; each row must truncate inside the scroll container.
const LONG_ACTIVITY_ITEMS: StrategicActivityItem[] = [
  { id: 1, kind: 'command', text: `npx ${'x'.repeat(280)} --flag=/absolute/path/with/no/spaces` },
  { id: 2, kind: 'file', text: `backend/app/${'y'.repeat(280)}/module.py` },
  { id: 3, kind: 'read', text: `https://example.com/${'z'.repeat(300)}` },
];

// Items with an in-flight last item (simulates a long-running tool call).
const IN_FLIGHT_ACTIVITY_ITEMS: StrategicActivityItem[] = [
  { id: 1, kind: 'command', text: 'bash scripts/test-backend.sh testing/test_orchestrator.py' },
  { id: 2, kind: 'tool', text: 'spawn subagent: review PR #13245', inFlight: true, ts: Date.now() - 5000 },
];

describe('StrategicLiveTurn — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  function mountLiveTurn() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    return act(async () => {
      r.render(
        <>
          <StrategicStreamingBubble segments={[{ kind: 'reply', text: LONG_UNBROKEN_TEXT }]} />
          <StrategicActivityStrip items={LONG_ACTIVITY_ITEMS} latestLifecycle="working" />
        </>,
      );
    });
  }

  function mountReasoningTurn() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    return act(async () => {
      r.render(<StrategicStreamingBubble segments={REASONING_PLUS_REPLY_SEGMENTS} />);
    });
  }

  function mountResidentWidget(state: ResidentActivityState) {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    return act(async () => {
      r.render(<StrategicResidentActivityWidget state={state} />);
    });
  }

  function mountInFlightTurn() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    return act(async () => {
      r.render(
        <StrategicActivityStrip items={IN_FLIGHT_ACTIVITY_ITEMS} latestLifecycle="working" />,
      );
    });
  }

  test('streaming bubble + activity strip fit 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      await mountLiveTurn();
    });
  });

  test('streaming bubble + activity strip fit 320px without horizontal overflow', async () => {
    await expectNoOverflowAt(320, async () => {
      await mountLiveTurn();
    });
  });

  test('streaming reply text wraps (not silently clipped) at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      await mountLiveTurn();
    });
  });

  // StrategicLiveTurn is a read-only live preview with no interactive controls.
  test('touch target (vacuous — no interactive controls)', async () => {
    await expectMinTouchTargetAt(
      375,
      async () => {
        await mountLiveTurn();
      },
      { selector: 'none' },
    );
  });

  test('in-flight last item renders "running… (Ns)" indicator at 768px without overflow', async () => {
    await expectNoOverflowAt(768, async () => {
      await mountInFlightTurn();
    });
    // Verify the running indicator text is present in the DOM.
    const indicator = host?.querySelector('[data-testid="strategic-activity-strip"]');
    const text = indicator?.textContent ?? '';
    if (!text.includes('running…')) {
      throw new Error(`Expected "running…" in activity strip but got: "${text.slice(0, 200)}"`);
    }
  });

  test('in-flight indicator fits 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      await mountInFlightTurn();
    });
  });

  test('reasoning renders in its own muted block, set apart from the reply, at 900px', async () => {
    await expectNoOverflowAt(900, async () => {
      await mountReasoningTurn();
    });
    const reasoning = host?.querySelector('[data-testid="strategic-streaming-reasoning"]');
    const reply = host?.querySelector('[data-testid="strategic-streaming-reply"]');
    if (!reasoning) throw new Error('expected a distinct reasoning block set apart from the reply');
    if (!reply) throw new Error('expected the primary reply block');
    if (!(reasoning.textContent ?? '').startsWith('Reasoning')) {
      throw new Error(`expected the reasoning block to be labelled "Reasoning", got: "${(reasoning.textContent ?? '').slice(0, 40)}"`);
    }
    // Reasoning must not bleed into the reply block (never mistaken for the answer).
    if ((reply.textContent ?? '').includes('Weighing the trade-offs')) {
      throw new Error('reasoning text leaked into the reply block');
    }
  });

  test('reasoning-plus-reply turn fits 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      await mountReasoningTurn();
    });
  });

  test('resident widget — executing shows the running command + live timer at 900px', async () => {
    await expectNoOverflowAt(900, async () => {
      await mountResidentWidget({
        phase: 'executing',
        label: 'Working',
        commandText: `bash scripts/${'e'.repeat(280)}/test-backend.sh`,
        startMs: Date.now() - 4000,
      });
    });
    const widget = host?.querySelector('[data-testid="strategic-resident-activity"]');
    if (widget?.getAttribute('data-phase') !== 'executing') throw new Error('expected executing phase');
    if (!(widget?.textContent ?? '').includes('running…')) {
      throw new Error(`expected a live timer in executing state, got: "${(widget?.textContent ?? '').slice(0, 120)}"`);
    }
  });

  test('resident widget — idle shows waiting copy and NO ticking timer at 900px', async () => {
    await expectNoOverflowAt(900, async () => {
      await mountResidentWidget({ phase: 'idle', label: 'Waiting for direction', endMs: Date.now() });
    });
    const widget = host?.querySelector('[data-testid="strategic-resident-activity"]');
    if (widget?.getAttribute('data-phase') !== 'idle') throw new Error('expected idle phase');
    const text = widget?.textContent ?? '';
    if (!text.includes('Waiting for direction')) throw new Error(`expected idle copy, got: "${text}"`);
    if (text.includes('running…')) throw new Error('idle state must not render a ticking timer');
  });

  test('resident widget — dead/ended shows frozen ended presentation, no timer, at 900px', async () => {
    await expectNoOverflowAt(900, async () => {
      await mountResidentWidget({ phase: 'dead', label: 'Session ended', endMs: Date.now() - 60000 });
    });
    const widget = host?.querySelector('[data-testid="strategic-resident-activity"]');
    if (widget?.getAttribute('data-phase') !== 'dead') throw new Error('expected dead phase');
    const text = widget?.textContent ?? '';
    if (!text.includes('Session ended')) throw new Error(`expected ended copy, got: "${text}"`);
    if (text.includes('running…')) throw new Error('dead state must never render a ticking timer');
  });

  test('resident widget — dead state fits 320px without horizontal overflow', async () => {
    await expectNoOverflowAt(320, async () => {
      await mountResidentWidget({ phase: 'dead', label: 'Session ended', endMs: Date.now() });
    });
  });
});
