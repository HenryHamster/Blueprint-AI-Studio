import { describe, it, expect } from 'vitest';
import type { Task } from '../components/TaskCard';
import { hasTransitiveCycle } from './taskGraph';

function makeTask(id: string, dependsOn: string[] = []): Task {
  return {
    id,
    title: id,
    description: '',
    status: 'QUEUED',
    created_by: 'test',
    requires_approval: false,
    created_at: '2026-01-01T00:00:00Z',
    depends_on: dependsOn.length > 0 ? dependsOn : undefined,
  } as Task;
}

describe('hasTransitiveCycle', () => {
  it('returns false when there are no dependencies', () => {
    const tasks = [makeTask('a'), makeTask('b'), makeTask('c')];
    expect(hasTransitiveCycle('a', 'b', tasks)).toBe(false);
  });

  it('returns false when the target has no depends_on', () => {
    const tasks = [makeTask('a'), makeTask('b')];
    expect(hasTransitiveCycle('a', 'b', tasks)).toBe(false);
  });

  it('detects a direct cycle: target depends on start', () => {
    // If we ask "would adding start → target create a cycle?"
    // and target already depends on start, that is a cycle.
    const tasks = [makeTask('a'), makeTask('b', ['a'])];
    expect(hasTransitiveCycle('a', 'b', tasks)).toBe(true);
  });

  it('detects a transitive cycle through a chain', () => {
    // chain: b → c → a — so adding a → b would create a cycle
    const tasks = [makeTask('a'), makeTask('b', ['c']), makeTask('c', ['a'])];
    expect(hasTransitiveCycle('a', 'b', tasks)).toBe(true);
  });

  it('returns false when the dependency chain does not reach the start', () => {
    // b → c → d (no connection back to a)
    const tasks = [makeTask('a'), makeTask('b', ['c']), makeTask('c', ['d']), makeTask('d')];
    expect(hasTransitiveCycle('a', 'b', tasks)).toBe(false);
  });

  it('handles an empty task list without throwing', () => {
    expect(hasTransitiveCycle('a', 'b', [])).toBe(false);
  });

  it('does not falsely detect a cycle when start and target are the same', () => {
    // Asking whether a → a is a cycle: the BFS starts at 'a' (target) and checks
    // if its depends_on contains 'a' (start). Here 'a' has no deps, so false.
    const tasks = [makeTask('a')];
    expect(hasTransitiveCycle('a', 'a', tasks)).toBe(false);
  });

  it('handles a task depending on itself (self-loop)', () => {
    // a depends on a — BFS from 'a' traverses to 'a', which is startId
    const tasks = [makeTask('a', ['a'])];
    expect(hasTransitiveCycle('a', 'a', tasks)).toBe(true);
  });

  it('respects the maxDepth guard on deeply nested chains', () => {
    // Build a chain longer than the default maxDepth (10): a → 1 → 2 → ... → 11
    const deepTasks: Task[] = [makeTask('a')];
    for (let i = 0; i <= 12; i++) {
      const prevId = i === 0 ? 'a' : String(i - 1);
      deepTasks.push(makeTask(String(i), [prevId]));
    }
    // deepTasks[12] is at depth 12, which exceeds maxDepth=10
    // The BFS should stop before reaching 'a' and return false (no cycle detected
    // past max depth — the function treats it as absent for safety).
    expect(hasTransitiveCycle('a', String(12), deepTasks)).toBe(false);
  });

  it('visits each node only once (handles diamond-shaped graphs without infinite loops)', () => {
    // Diamond: target=b → {c, d}, c → e, d → e, e → a
    const tasks = [
      makeTask('a'),
      makeTask('b', ['c', 'd']),
      makeTask('c', ['e']),
      makeTask('d', ['e']),
      makeTask('e', ['a']),
    ];
    expect(hasTransitiveCycle('a', 'b', tasks)).toBe(true);
  });

  it('handles tasks with no depends_on gracefully (undefined)', () => {
    const task = { ...makeTask('b'), depends_on: undefined };
    expect(hasTransitiveCycle('a', 'b', [makeTask('a'), task])).toBe(false);
  });
});
