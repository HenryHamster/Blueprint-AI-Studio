import { describe, expect, it, vi } from 'vitest';

import { BrowserPlayoutTracker } from './browserPlayoutTracker';

describe('BrowserPlayoutTracker', () => {
  it('requires audible browser frames before reporting source lifecycle', () => {
    const emit = vi.fn();
    const tracker = new BrowserPlayoutTracker(emit);
    tracker.source('p1', 'started');
    tracker.source('p1', 'completed');
    tracker.audioLevel(false, 0);
    expect(emit).not.toHaveBeenCalled();
    tracker.audioLevel(true, 10);
    expect(emit).toHaveBeenCalledWith({
      phraseId: 'p1', event: 'started', at: new Date(10).toISOString(),
    });
    tracker.audioLevel(false, 20);
    tracker.audioLevel(false, 141);
    expect(emit).toHaveBeenLastCalledWith({
      phraseId: 'p1', event: 'completed', at: new Date(141).toISOString(),
    });
  });

  it('reports interruption only after rendered audio stops', () => {
    const emit = vi.fn();
    const tracker = new BrowserPlayoutTracker(emit);
    tracker.source('p2', 'started');
    tracker.audioLevel(true, 100);
    tracker.source('p2', 'interrupted');
    tracker.audioLevel(false, 110);
    expect(emit).toHaveBeenCalledTimes(1);
    tracker.audioLevel(false, 231);
    expect(emit.mock.calls.map(([event]) => event.event)).toEqual(['started', 'interrupted']);
  });
});
