import { describe, expect, test } from 'vitest';
import { ComposerRequestOwnershipGuard } from './composerRequestOwnership';

describe('composer request ownership', () => {
  test('late actor A submit cannot finalize actor B abandon in the same project', async () => {
    const guard = new ComposerRequestOwnershipGuard();
    let finishActorA!: () => void;
    const actorADeferred = new Promise<void>(resolve => { finishActorA = resolve; });
    const actorA = guard.begin('actor-a', 'project-1');
    guard.reset();
    const actorB = guard.begin('actor-b', 'project-1');

    finishActorA();
    await actorADeferred;
    expect(guard.finish(actorA)).toBe(false);
    expect(guard.active).toBe(true);
    expect(guard.finish(actorB)).toBe(true);
  });

  test('request generation prevents an older same-actor request finalizing its successor', () => {
    const guard = new ComposerRequestOwnershipGuard();
    const older = guard.begin('actor-a', 'project-1');
    const newer = guard.begin('actor-a', 'project-1');
    expect(guard.finish(older)).toBe(false);
    expect(guard.active).toBe(true);
    expect(guard.finish(newer)).toBe(true);
  });
});
